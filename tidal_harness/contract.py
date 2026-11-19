"""任务契约 + Agent 注册表 + 规划器。

契约是整套系统的"单一事实来源"：排队前校验，执行中保留，缓存隔离用它做键。
"""

from __future__ import annotations

import hashlib
import json
import keyword
import math
from dataclasses import dataclass, field, asdict
from typing import Any


class ContractError(ValueError):
    """契约不合法 —— 在进入队列之前就要拒绝。"""


@dataclass(frozen=True)
class TestCase:
    args: tuple
    expected: Any

    def to_json(self) -> dict:
        return {"args": list(self.args), "expected": self.expected}


@dataclass(frozen=True)
class TaskContract:
    """一次任务的全部前提。冻结（frozen）是为了防止执行途中被悄悄改掉。"""

    task_id: str
    description: str            # 自然语言
    function_name: str          # 函数任务的入口名，脚本使用空字符串
    tests: tuple[TestCase, ...]  # 用户给定的测试
    max_rounds: int = 3
    context_budget_tokens: int = 3000
    deadline_seconds: float = 120.0
    tenant: str = "default"
    model: str = "demo"
    task_type: str = "function"

    @classmethod
    def from_data(cls, data: dict, *, task_id: str, default_budget: int = 2000) -> TaskContract:
        """网页、HTTP 和 CLI 使用同一份任务格式。旧函数任务仍由 function_name 识别。"""
        if not isinstance(data, dict):
            raise ContractError("任务必须是 JSON 对象")
        description = data.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ContractError("请填写任务说明")
        kind = data.get("task_type", "function" if data.get("function_name") else "script")
        raw_cases = data.get("tests", [])
        if not isinstance(raw_cases, list):
            raise ContractError("tests 必须是用例列表")
        cases = []
        for item in raw_cases:
            if isinstance(item, dict) and kind == "script" and "stdout" in item:
                args, expected = [item.get("stdin", "")], item["stdout"]
            elif isinstance(item, dict) and "args" in item and "expected" in item:
                args, expected = item["args"], item["expected"]
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                args, expected = item
            else:
                raise ContractError("请为每个用例提供参数和期望结果")
            if not isinstance(args, (list, tuple)):
                raise ContractError("每个用例的参数应是列表")
            cases.append(TestCase(tuple(args), expected))
        contract = cls(task_id=task_id, description=description.strip(),
                       function_name=data.get("function_name", "") if kind == "function" else "",
                       tests=tuple(cases), task_type=kind,
                       max_rounds=int(data.get("max_rounds", 3)),
                       context_budget_tokens=int(data.get("context_budget_tokens", default_budget)),
                       deadline_seconds=float(data.get("deadline_seconds", 120)))
        contract.validate()
        return contract

    @property
    def is_add_demo(self) -> bool:
        if self.task_type != "function" or self.function_name != "add" or not self.tests:
            return False
        description = self.description.lower()
        if not any(term in description for term in ("两数之和", "加法", "相加", "两数和", "求和", "a + b", "a+b", "sum", "addition", "add two")):
            return False
        return all(len(t.args) == 2 and all(type(a) in (int, float) for a in t.args)
                   and type(t.expected) in (int, float) and t.expected == t.args[0] + t.args[1]
                   for t in self.tests)

    # ---------- 校验 ----------
    def validate(self) -> None:
        if not self.description.strip():
            raise ContractError("description 不能为空")
        if self.task_type not in ("function", "script"):
            raise ContractError("task_type 必须是 function（函数）或 script（脚本）")
        if self.task_type == "function" and (not isinstance(self.function_name, str) or not self.function_name.isidentifier() or keyword.iskeyword(self.function_name)):
            raise ContractError(f"函数名不合法：{self.function_name!r}")
        if self.task_type == "function" and not self.tests:
            raise ContractError("至少要有一个测试用例")
        if not (1 <= self.max_rounds <= 5):
            raise ContractError("max_rounds 必须在 1~5 之间")
        if self.context_budget_tokens < 256:
            raise ContractError("context_budget_tokens 太小（<256）")
        if not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0:
            raise ContractError("deadline_seconds 必须为正")
        for t in self.tests:
            if not isinstance(t.args, tuple):
                raise ContractError("测试参数必须是 tuple")
            if self.task_type == "script" and (len(t.args) > 1 or not all(isinstance(a, str) for a in t.args) or not isinstance(t.expected, str)):
                raise ContractError("脚本用例需要文本形式的标准输入和预期标准输出")

    # ---------- 身份 ----------
    def digest(self) -> str:
        """契约摘要：改一个字就变，用它做缓存隔离与版本守卫的依据。"""
        payload = json.dumps(
            {
                "description": self.description,
                "task_type": self.task_type,
                "function_name": self.function_name,
                "tests": [t.to_json() for t in self.tests],
                "max_rounds": self.max_rounds,
                "context_budget_tokens": self.context_budget_tokens,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_json(self) -> dict:
        d = asdict(self)
        d["tests"] = [t.to_json() for t in self.tests]
        d["digest"] = self.digest()
        return d


# ---------------------------------------------------------------- 注册表

@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str                  # planner / coder / reviewer
    capabilities: tuple[str, ...]
    instruction: str
    model: str | None = None


@dataclass
class AgentRegistry:
    agents: dict[str, AgentSpec] = field(default_factory=dict)

    def register(self, spec: AgentSpec) -> None:
        if spec.agent_id in self.agents:
            raise ValueError(f"agent 重复注册：{spec.agent_id}")
        self.agents[spec.agent_id] = spec

    def by_role(self, role: str) -> list[AgentSpec]:
        return [a for a in self.agents.values() if a.role == role]

    def by_capability(self, cap: str) -> list[AgentSpec]:
        return [a for a in self.agents.values() if cap in a.capabilities]

    def get(self, agent_id: str) -> AgentSpec:
        return self.agents[agent_id]

    @staticmethod
    def default() -> "AgentRegistry":
        r = AgentRegistry()
        r.register(AgentSpec(
            agent_id="planner",
            role="planner",
            capabilities=("plan",),
            instruction="根据任务与可用角色，选择编码者与审查者，并决定先测后审还是先审后测。",
        ))
        r.register(AgentSpec(
            agent_id="coder-a",
            role="coder",
            capabilities=("code", "python"),
            instruction="按原始任务与验收条件编写 Python 代码。函数任务提供指定函数，脚本任务提供可直接运行的完整脚本。只输出 JSON：{\"code\": \"...\", \"explanation\": \"...\"}",
        ))
        r.register(AgentSpec(
            agent_id="reviewer-b",
            role="reviewer",
            capabilities=("review", "python"),
            instruction="独立审查候选实现，输出 JSON：{\"approve\": true/false, \"issues\": [...]}",
        ))
        return r


# ---------------------------------------------------------------- 规划

@dataclass(frozen=True)
class Plan:
    coder: str
    reviewer: str
    order: tuple[str, str] = ("test", "review")   # 先确定性测试，再独立审查
    reason: str = ""


class Planner:
    """规划只做一件事：把契约映射到一个**合法**的角色组合与检查顺序。

    合法 = coder/reviewer 都在注册表里且角色正确；顺序只能是 ("test","review")。
    真接 LLM 时，LLM 的输出也要过这层校验，不合法的组合直接回退到默认。
    """

    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    def plan(self, contract: TaskContract, hint: dict | None = None) -> Plan:
        coders = [a.agent_id for a in self.registry.by_role("coder")]
        reviewers = [a.agent_id for a in self.registry.by_role("reviewer")]
        if not coders:
            raise ContractError("没有可用的 coder")
        if not reviewers:
            raise ContractError("没有可用的 reviewer")

        coder, reviewer = coders[0], reviewers[0]
        reason = "默认规划：唯一可用角色"
        if hint:
            if hint.get("coder") in coders:
                coder = hint["coder"]
                reason = "LLM 规划的 coder 通过合法性校验"
            if hint.get("reviewer") in reviewers and hint["reviewer"] != coder:
                reviewer = hint["reviewer"]
                reason = "LLM 规划的 reviewer 通过合法性校验"
        # 检查顺序：默认先测后审；允许 hint 指定，但只接受这两个合法排列
        order: tuple[str, str] = ("test", "review")
        if hint and hint.get("order") in (["test", "review"], ["review", "test"]):
            order = tuple(hint["order"])  # type: ignore[assignment]
            reason += f"；顺序由规划指定：{order[0]}→{order[1]}"
        return Plan(coder=coder, reviewer=reviewer, order=order, reason=reason)
