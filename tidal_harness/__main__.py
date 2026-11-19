"""命令行入口。

用法：
    python -m tidal_harness demo                 # 跑内置任务，演示失败→修正→通过
    python -m tidal_harness demo --script clean  # 一次就过
    python -m tidal_harness run --file task.json # 跑一个自定义任务
    python -m tidal_harness serve --demo         # 起本地服务（浏览器可看任务与事件）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .contract import AgentRegistry, ContractError, TaskContract, TestCase
from .engine import Engine
from .runtime import CandidateCache, ScriptedDemoModel, Store

DATA_DIR = os.environ.get("TIDAL_DATA", os.path.join(os.getcwd(), "data"))


def build_engine(script: str = "fail-first", data_dir: str | None = None) -> Engine:
    os.makedirs(data_dir or DATA_DIR, exist_ok=True)
    store = Store(os.path.join(data_dir or DATA_DIR, "tidal.db"))
    store.mark_interrupted()          # 上次没跑完的任务明确标成中断
    registry = AgentRegistry.default()
    model = ScriptedDemoModel(script)
    return Engine(registry, model, store, CandidateCache(), log=print)


def demo_contract(task_id: str = "demo-add") -> TaskContract:
    return TaskContract(
        task_id=task_id,
        description="实现一个函数 add(a, b)，返回两数之和；要求支持负数与小数。",
        function_name="add",
        tests=(TestCase((1, 2), 3), TestCase((-1, 5), 4), TestCase((0.5, 0.5), 1.0)),
        max_rounds=3,
        context_budget_tokens=2000,
        deadline_seconds=60,
        tenant="demo",
        model="scripted",
    )


def print_events(engine: Engine, task_id: str) -> None:
    for e in engine.store.events(task_id):
        payload = json.loads(e["payload"]) if e["payload"] else {}
        brief = json.dumps(payload, ensure_ascii=False)
        print(f"  [{e['seq']:>2}] {e['stage']:<14} {brief[:160]}")


def cmd_demo(args) -> int:
    engine = build_engine(args.script)
    c = demo_contract(f"demo-{time.time_ns()}")
    print(f"== 任务 {c.task_id} ==")
    print(f"   {c.description}")
    print(f"   测试 {len(c.tests)} 条，最多 {c.max_rounds} 轮，预算 {c.context_budget_tokens} tokens\n")
    t0 = time.time()
    result = engine.run(c)
    print(f"\n== 事件 ==")
    print_events(engine, c.task_id)
    print(f"\n== 结果 ==")
    print(f"   状态 {result.status} · 轮次 {len(result.rounds)} · 耗时 {time.time() - t0:.3f}s")
    for r in result.rounds:
        gates = []
        gates.append(f"静态{'过' if r.static_ok else '挂'}")
        gates.append(f"测试{'过' if r.tests_ok else '挂'}")
        gates.append(("审查通过" if r.review_ok else "审查驳回") if r.tests_ok else "审查未执行")
        print(f"   第 {r.round} 轮：{' / '.join(gates)}" + (f"  意见：{'；'.join(r.review_issues)}" if r.review_issues else ""))
    if result.rounds and result.rounds[-1].accepted:
        print(f"\n   最终代码：\n{indent(result.rounds[-1].code, '   ')}")
    return 0 if result.ok else 1


def indent(text: str, pad: str) -> str:
    return "\n".join(pad + line for line in text.strip().splitlines())


def cmd_run(args) -> int:
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    tests = tuple(TestCase(tuple(t["args"]), t["expected"]) for t in data["tests"])
    c = TaskContract(
        task_id=data.get("task_id", f"task-{int(time.time())}"),
        description=data["description"],
        function_name=data["function_name"],
        tests=tests,
        max_rounds=int(data.get("max_rounds", 3)),
        context_budget_tokens=int(data.get("context_budget_tokens", 2000)),
        deadline_seconds=float(data.get("deadline_seconds", 120)),
        tenant=data.get("tenant", "default"),
        model=data.get("model", "scripted"),
    )
    engine = build_engine(args.script)
    result = engine.run(c)
    print(json.dumps({"task_id": c.task_id, "status": result.status,
                      "rounds": len(result.rounds), "ok": result.ok}, ensure_ascii=False))
    return 0 if result.ok else 1


# ---------------------------------------------------------------- server

HTML = """<!doctype html><meta charset=utf-8><title>Tidal Harness</title>
<style>body{font:14px/1.6 system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 16px;color:#222}
h1{font-size:20px}.row{border:1px solid #e3e8ef;border-radius:8px;padding:10px 12px;margin:8px 0}
.s{color:#666;font-size:12px}.ok{color:#0a7a3d}.fail{color:#b42318}</style>
<h1>Tidal Harness · 任务与事件</h1><div id=list>加载中…</div>
<script>
async function load(){
  const r = await fetch('/api/tasks'); const d = await r.json();
  document.getElementById('list').innerHTML = d.tasks.map(t =>
    `<div class=row><b>${t.task_id}</b> <span class="s">${t.status} · ${t.rounds} 轮</span>
     <div class=s>${t.detail||''}</div></div>`).join('') || '<p>还没有任务</p>';
}
load(); setInterval(load, 3000);
</script>"""


def make_handler(engine: Engine):
    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/tasks":
                return self._send(200, json.dumps({"tasks": engine.store.tasks()}, ensure_ascii=False).encode())
            if self.path.startswith("/api/tasks/"):
                tid = self.path.rsplit("/", 1)[-1]
                task = engine.store.task(tid)
                if not task:
                    return self._send(404, b'{"error":"not found"}')
                return self._send(200, json.dumps({"task": task, "events": engine.store.events(tid)},
                                                  ensure_ascii=False).encode())
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):  # noqa: N802
            if self.path != "/api/tasks":
                return self._send(404, b'{"error":"not found"}')
            n = int(self.headers.get("content-length", 0))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                tests = tuple(TestCase(tuple(t["args"]), t["expected"]) for t in data["tests"])
                c = TaskContract(
                    task_id=data.get("task_id", f"http-{int(time.time()*1000)}"),
                    description=data["description"],
                    function_name=data["function_name"],
                    tests=tests,
                )
                c.validate()
            except Exception as e:  # noqa: BLE001
                return self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode())
            threading.Thread(target=engine.run, args=(c,), daemon=True).start()
            return self._send(202, json.dumps({"task_id": c.task_id, "status": "queued"}).encode())

        def log_message(self, *a):  # 静音
            return

    return H


def cmd_serve(args) -> int:
    from .web import ChatBrain, ChatStore, Hub, bridge_events, make_server
    from .runtime import OpenAICompatibleModel

    engine = build_engine(args.script)
    base_url = None if args.demo else os.environ.get("TIDAL_MODEL_BASE")
    if base_url:
        engine.model = OpenAICompatibleModel(base_url, os.environ.get("TIDAL_MODEL_KEY", ""),
                                             os.environ.get("TIDAL_MODEL_NAME", "deepseek-chat"))
    hub = Hub()
    chat = ChatStore(os.path.join(DATA_DIR, "tidal.db"))
    bridge_events(engine, hub, chat, demo=args.demo)
    brain = ChatBrain(engine, demo=not bool(base_url))

    srv = make_server(engine, hub, chat, brain, host=args.host, port=args.port)
    if args.port == 0:
        print(f"READY {srv.server_port}", flush=True)
    print(f"面板已启动： http://{args.host}:{srv.server_port}   (Ctrl+C 退出)")
    print(f"数据目录： {DATA_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tidal_harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="跑内置任务")
    d.add_argument("--script", default="fail-first", choices=["fail-first", "clean"])
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="跑自定义任务")
    r.add_argument("--file", required=True)
    r.add_argument("--script", default="clean", choices=["fail-first", "clean"])
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("serve", help="起本地服务")
    s.add_argument("--demo", action="store_true")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--script", default="fail-first", choices=["fail-first", "clean"])
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
