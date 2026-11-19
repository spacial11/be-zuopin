"""引擎：确定性验证 + 独立审查 + PDCA 循环 + 有界调度。

这是"另一条独立检查并提建议"真正落地的地方：
  · Agent A 产出候选；
  · 确定性验证先跑（AST + 真测试）；
  · Agent B 独立审查（不同角色、不同提示词），给出 批准/驳回 + 问题列表；
  · 驳回意见 + 失败用例一起回灌给 A，最多三轮；
  · 两道都过才算成功。
"""

from __future__ import annotations

import ast
import itertools
import json
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .contract import AgentRegistry, Plan, Planner, TaskContract, TestCase
from .runtime import CandidateCache, ContextWindow, ScriptedDemoModel, Store
from .inner import RoundInner, facts_from_contract

# ---------------------------------------------------------------- 受限 AST

ALLOWED_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AugAssign, ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.Expr, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.Name,
    ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Subscript,
    ast.Slice, ast.IfExp, ast.JoinedStr, ast.FormattedValue, ast.ListComp,
    ast.comprehension, ast.Attribute, ast.keyword,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot, ast.Subscript, ast.Index if hasattr(ast, "Index") else ast.expr,
}
FORBIDDEN_NAMES = {
    "exec", "eval", "compile", "open", "input", "__import__", "globals", "locals",
    "getattr", "setattr", "delattr", "vars", "breakpoint", "memoryview",
}
FORBIDDEN_ATTRS = {"__class__", "__bases__", "__subclasses__", "__globals__", "__dict__", "__code__"}
MAX_NODES = 400
MAX_CONST_INT = 10 ** 9


@dataclass
class StaticReport:
    ok: bool
    issues: list[str] = field(default_factory=list)
    nodes: int = 0


def validate_static(code: str, function_name: str) -> StaticReport:
    """受限 AST：只能写纯函数，不许导入、不许 exec、不许碰文件与网络。"""
    issues: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return StaticReport(False, [f"语法错误：{e.msg}（第 {e.lineno} 行）"])
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        issues.append(f"AST 节点过多：{len(nodes)} > {MAX_NODES}")
    for node in nodes:
        if type(node) not in ALLOWED_NODES:
            issues.append(f"不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
            issues.append("不允许 import")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            issues.append(f"不允许使用 {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            issues.append(f"不允许访问属性 {node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            issues.append(f"不允许调用 {node.func.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and abs(node.value) > MAX_CONST_INT:
            issues.append("常量过大")
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if function_name not in funcs:
        issues.append(f"没有定义函数 {function_name}")
    return StaticReport(not issues, sorted(set(issues)), len(nodes))


# ---------------------------------------------------------------- 真跑测试

RUNNER = r'''
import json, sys, signal
def _fail(*a, **k): raise RuntimeError("sandbox: forbidden builtin")
payload = json.loads(sys.stdin.read())
ns = {"__builtins__": {
    "abs": abs, "min": min, "max": max, "len": len, "sum": sum, "range": range,
    "int": int, "float": float, "str": str, "bool": bool, "list": list, "dict": dict,
    "tuple": tuple, "round": round, "sorted": sorted, "enumerate": enumerate, "zip": zip,
    "ValueError": ValueError, "TypeError": TypeError, "Exception": Exception, "isinstance": isinstance,
}}
ns["__builtins__"] = dict(ns["__builtins__"])
ns["__builtins__"]["__import__"] = _fail
try:
    exec(compile(payload["code"], "<candidate>", "exec"), ns)
    fn = ns[payload["function_name"]]
    results = []
    for case in payload["tests"]:
        try:
            got = fn(*case["args"])
            results.append({"ok": got == case["expected"], "got": repr(got), "expected": repr(case["expected"])})
        except Exception as e:
            results.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
    print(json.dumps({"ok": True, "results": results}))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
'''


@dataclass
class TestReport:
    ok: bool
    passed: int
    total: int
    details: list[dict] = field(default_factory=list)
    error: str = ""


def run_tests(code: str, contract: TaskContract, timeout: float = 10.0) -> TestReport:
    payload = {
        "code": code,
        "function_name": contract.function_name,
        "tests": [{"args": list(t.args), "expected": t.expected} for t in contract.tests],
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestReport(False, 0, len(contract.tests), error=f"执行超时（>{timeout}s）")
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        data = json.loads(out.splitlines()[-1]) if out else {"ok": False, "error": "无输出"}
    except Exception:
        return TestReport(False, 0, len(contract.tests), error=(out or proc.stderr.decode())[:200])
    if not data.get("ok"):
        return TestReport(False, 0, len(contract.tests), error=data.get("error", "执行失败"))
    results = data["results"]
    passed = sum(1 for r in results if r.get("ok"))
    return TestReport(passed == len(results), passed, len(results), results)


# ---------------------------------------------------------------- 候选解析

def parse_candidate(text: str) -> tuple[str, str]:
    """从模型输出里取 {code, explanation}。容忍 ```json 围栏。"""
    raw = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
        return str(data.get("code", "")), str(data.get("explanation", ""))
    except Exception:
        return raw, ""


def parse_review(text: str) -> tuple[bool, list[str]]:
    """从审查输出里取 {approve, issues}。解析失败一律视为驳回。"""
    raw = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
        approve = data.get("approve") is True
        issues = [str(x) for x in (data.get("issues") or [])]
        return approve, issues
    except Exception:
        return False, ["审查输出不是合法 JSON，按驳回处理"]


# ---------------------------------------------------------------- 引擎

@dataclass
class RoundRecord:
    round: int
    code: str
    explanation: str
    static_ok: bool
    static_issues: list[str]
    tests_ok: bool
    tests: list[dict]
    review_ok: bool
    review_issues: list[str]
    accepted: bool


@dataclass
class RunResult:
    task_id: str
    ok: bool
    rounds: list[RoundRecord]
    status: str
    from_cache: bool = False
    duration: float = 0.0


class Reviewer:
    """独立审查者：换一个角色、换一段提示词，不是让同一个模型自我表扬。"""

    def __init__(self, model, agent_id: str):
        self.model = model
        self.agent_id = agent_id

    def review(self, contract: TaskContract, code: str, test_summary: str) -> tuple[bool, list[str], list[dict]]:
        if isinstance(self.model, ScriptedDemoModel) and contract.function_name == "add":
            cases = (TestCase((0.5, 0.25), 0.75), TestCase((-2, 0.5), -1.5))
            report = run_tests(code, replace(contract, tests=cases))
            evidence = [
                {"args": list(case.args), **result}
                for case, result in zip(cases, report.details)
            ]
            issues = [
                f"add({row['args'][0]}, {row['args'][1]}) 应得 {row['expected']}，实际 {row['got']}"
                if "got" in row else row["error"]
                for row in evidence if not row["ok"]
            ]
            if report.error:
                issues.append(report.error)
            return report.ok, issues, evidence
        prompt = (
            "你是独立审查者，不是写代码的人。请只根据任务要求与下面的候选实现判断它是否满足要求。\n"
            f"任务：{contract.description}\n函数名：{contract.function_name}\n"
            f"测试用例：{json.dumps([t.to_json() for t in contract.tests], ensure_ascii=False)}\n"
            f"测试执行结果：{test_summary}\n"
            f"候选实现：\n```python\n{code}\n```\n"
            '只输出 JSON：{"approve": true/false, "issues": ["..."]}。'
        )
        out = self.model.complete(
            [{"role": "user", "content": prompt}], role="reviewer", contract=contract
        )
        approved, issues = parse_review(out)
        return approved, issues, []


class Engine:
    def __init__(self, registry: AgentRegistry, model, store: Store,
                 cache: CandidateCache | None = None, log=print):
        self.registry = registry
        self.model = model
        self.store = store
        self.cache = cache or CandidateCache()
        self.planner = Planner(registry)
        self.log = log

    def _recent_openings(self) -> list[str]:
        return list(getattr(self, "_openings", []))[-5:]

    def _note_opening(self, code: str) -> None:
        first = (code or "").strip().splitlines()[0] if (code or "").strip() else ""
        if not first:
            return
        arr = list(getattr(self, "_openings", []))
        arr.append(first)
        self._openings = arr[-10:]

    def run(self, contract: TaskContract, use_cache: bool = True) -> RunResult:
        contract.validate()
        started = time.time()
        digest = contract.digest()
        self.store.upsert_task(contract.task_id, digest, "running", 0, title=contract.description)
        self.store.event(contract.task_id, "queued", {"digest": digest})

        plan = self.planner.plan(contract)
        self.store.event(contract.task_id, "plan", {"coder": plan.coder, "reviewer": plan.reviewer,
                                                    "order": list(plan.order), "reason": plan.reason})

        # 缓存：命中也要用当前测试重跑一遍
        ckey = CandidateCache.key_for(digest, contract.model, contract.tenant)
        entry = self.cache.get(ckey) if use_cache else None
        if entry:
            t = run_tests(entry.code, contract)
            if t.ok:
                self.cache.bump(ckey)
                self.store.event(contract.task_id, "cache-hit", {"tests": f"{t.passed}/{t.total}"})
                self.store.upsert_task(contract.task_id, digest, "succeeded", 0, {"fromCache": True})
                return RunResult(contract.task_id, True, [], "succeeded", True, time.time() - started)
            self.cache.evict(ckey)
            self.store.event(contract.task_id, "cache-stale", {"reason": "命中但当前测试不过，已淘汰"})

        model = self.model
        if isinstance(model, ScriptedDemoModel):
            model = ScriptedDemoModel(model.script)
        reviewer = Reviewer(model, plan.reviewer)
        coder_spec = self.registry.get(plan.coder)
        history: list[dict] = []
        feedback = ""
        rounds: list[RoundRecord] = []
        revision = 1

        for rnd in range(1, contract.max_rounds + 1):
            window = ContextWindow(contract.context_budget_tokens)
            built = window.build(
                system=coder_spec.instruction,
                task=contract.description,
                current=feedback or "（第一轮，没有反馈）",
                history=history,
            )
            self.store.event(contract.task_id, "do", {"round": rnd, "context_tokens": built.used_tokens,
                                                      "omitted": built.omitted})
            facts = facts_from_contract(contract)
            avoid = self._recent_openings()
            rich = RoundInner(round=rnd, facts_locked=facts, avoid_openings=avoid,
                              context_tokens=built.used_tokens, omitted=list(built.omitted),
                              note="mood 字段在本版本恒为空：不做情绪系统，见白皮书 §6")
            self.store.event(contract.task_id, "inner", rich.to_json())

            out = model.complete(built.messages, role="coder", contract=contract)
            code, explanation = parse_candidate(out)
            history.append({"seq": len(history) + 1, "role": "assistant", "content": out[:500]})

            static = validate_static(code, contract.function_name)
            self.store.event(contract.task_id, "check-static",
                             {"round": rnd, "ok": static.ok, "issues": static.issues, "nodes": static.nodes})

            tests = TestReport(False, 0, len(contract.tests), error="未执行（静态检查未通过）")
            if static.ok:
                tests = run_tests(code, contract)
            self.store.event(contract.task_id, "check-tests",
                             {"round": rnd, "ok": tests.ok, "passed": tests.passed,
                              "total": tests.total, "error": tests.error,
                              "candidate": code, "details": tests.details})

            review_ok, review_issues = False, []
            if static.ok and tests.ok:
                summary = f"{tests.passed}/{tests.total} 通过"
                review_ok, review_issues, evidence = reviewer.review(contract, code, summary)
                self.store.event(contract.task_id, "check-review",
                                 {"round": rnd, "ok": review_ok, "issues": review_issues, "checks": evidence})

            self._note_opening(code)
            rec = RoundRecord(rnd, code, explanation, static.ok, static.issues,
                              tests.ok, tests.details, review_ok, review_issues,
                              static.ok and tests.ok and review_ok)
            rounds.append(rec)

            if rec.accepted:
                # 只有双门都过才允许写缓存
                self.cache.put(ckey, code, explanation)
                self.store.upsert_task(contract.task_id, digest, "succeeded", rnd,
                                       {"code": code, "explanation": explanation, "revision": revision})
                self.store.event(contract.task_id, "act", {"round": rnd, "result": "accepted"})
                return RunResult(contract.task_id, True, rounds, "succeeded", False, time.time() - started)

            # 失败：把审查意见 + 失败用例一起回灌
            parts = []
            if static.issues:
                parts.append("静态检查未通过：" + "；".join(static.issues))
            if not tests.ok:
                failed = [d for d in tests.details if not d.get("ok")] or [{"error": tests.error}]
                parts.append("测试未通过：" + json.dumps(failed[:3], ensure_ascii=False))
            if review_issues:
                parts.append("独立审查意见：" + "；".join(review_issues))
            feedback = "上一轮被打回，问题如下：\n- " + "\n- ".join(parts) + "\n请只输出修正后的 JSON。"
            revision += 1
            self.store.event(contract.task_id, "act", {"round": rnd, "result": "rejected", "feedback": feedback[:300]})

        self.store.upsert_task(contract.task_id, digest, "failed", contract.max_rounds,
                               {"reason": "超过最大轮次仍未通过"})
        return RunResult(contract.task_id, False, rounds, "failed", False, time.time() - started)


# ---------------------------------------------------------------- 调度

class TaskScheduler:
    """有界并发 + 优先级 + 取消。真接多任务时用它，demo 里可以只开 1 个 worker。"""

    def __init__(self, engine: Engine, workers: int = 2, log=print):
        self.engine = engine
        self.workers = max(1, workers)
        self.log = log
        self.q: queue.PriorityQueue = queue.PriorityQueue(maxsize=64)
        self._sequence = itertools.count()
        self._submit_lock = threading.Lock()
        self.cancelled: set[str] = set()
        self.results: dict[str, RunResult] = {}
        self._threads: list[threading.Thread] = []

    def submit(self, contract: TaskContract, priority: int = 5, use_cache: bool = True) -> None:
        contract.validate()
        if not 0 <= priority <= 9:
            raise ValueError("优先级取值为 0 至 9")
        with self._submit_lock:
            if self.engine.store.task(contract.task_id):
                raise FileExistsError("任务编号已存在，请使用新的编号")
            if self.q.full():
                raise queue.Full
            self.engine.store.upsert_task(contract.task_id, contract.digest(), "queued", title=contract.description)
            self.engine.store.event(contract.task_id, "scheduled",
                                    {"priority": priority, "workers": self.workers})
            self.q.put_nowait((priority, next(self._sequence), contract, use_cache))

    def cancel(self, task_id: str) -> None:
        self.cancelled.add(task_id)

    def start(self) -> None:
        for _ in range(self.workers):
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self) -> None:
        while True:
            _, _, contract, use_cache = self.q.get()
            try:
                if contract.task_id in self.cancelled:
                    self.engine.store.event(contract.task_id, "cancelled")
                    self.engine.store.upsert_task(contract.task_id, contract.digest(), "cancelled")
                    self.cancelled.discard(contract.task_id)
                else:
                    self.engine.run(contract, use_cache=use_cache)
            except Exception as e:  # noqa: BLE001
                reason = f"执行异常（{type(e).__name__}）"
                self.engine.store.event(contract.task_id, "error", {"reason": reason})
                self.engine.store.upsert_task(contract.task_id, contract.digest(), "failed",
                                              detail={"reason": reason})
                self.log(f"任务 {contract.task_id}：{reason}")
            finally:
                self.q.task_done()

    def join(self, timeout: float | None = None) -> None:
        with self.q.all_tasks_done:
            self.q.all_tasks_done.wait_for(lambda: self.q.unfinished_tasks == 0, timeout)
