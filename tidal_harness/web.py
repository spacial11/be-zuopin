"""浏览器面板：直接和助手对话 + 主动插话 + 任务与事件。

落点说明：参赛版不依赖 QQ、不依赖任何外部机器人框架，
所有交互都在这个页面里完成：
  · 左边：提交任务、看阶段事件（排队/规划/生成/静态/测试/审查/接受）
  · 右边：和助手对话；任务有动静时它会自己插一句
零依赖：http.server + Server-Sent Events + 原生 JS，不引任何前端库。
"""

from __future__ import annotations

import ipaddress
import json
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from . import predict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============================================================ 消息中心

class Hub:
    """简单的发布订阅：每个浏览器一个队列，SSE 从队列里取事件。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients: list[queue.Queue] = []
        self.backlog: list[dict] = []      # 最近 200 条，刷新后能补上

    def publish(self, event: dict) -> None:
        with self.lock:
            self.backlog.append(event)
            self.backlog = self.backlog[-200:]
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.clients.remove(q)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            for e in self.backlog[-50:]:
                try:
                    q.put_nowait(e)
                except queue.Full:
                    break
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


# ============================================================ 对话存储

class ChatStore:
    def __init__(self, db_path: str):
        self._db_lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS chat(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, at REAL NOT NULL)""")
        # 建议的曝光与接受：用于算"预测命中率"，进面板
        self.conn.execute("""CREATE TABLE IF NOT EXISTS suggestions(
            id TEXT NOT NULL, kind TEXT NOT NULL, at REAL NOT NULL,
            accepted INTEGER NOT NULL DEFAULT 0)""")
        self.conn.commit()

    def note_suggestion(self, sid: str, kind: str, accepted: bool) -> None:
        with self._db_lock:
            self.conn.execute("INSERT INTO suggestions(id,kind,at,accepted) VALUES(?,?,?,?)",
                              (sid, kind, time.time(), 1 if accepted else 0))
            self.conn.commit()

    def suggestion_stats(self) -> dict:
        with self._db_lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(accepted),0) AS a FROM suggestions").fetchone()
        shown, acc = int(row["n"]), int(row["a"])
        return {"shown": shown, "accepted": acc,
                "rate": round(acc / shown, 3) if shown else 0.0}

    def add(self, role: str, text: str, kind: str = "chat") -> dict:
        at = time.time()
        with self._db_lock:
            cur = self.conn.execute("INSERT INTO chat(role,kind,text,at) VALUES(?,?,?,?)",
                                    (role, kind, text, at))
            self.conn.commit()
        return {"id": cur.lastrowid, "role": role, "kind": kind, "text": text, "at": at}

    def recent(self, limit: int = 60) -> list[dict]:
        with self._db_lock:
            rows = self.conn.execute("SELECT * FROM chat ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]


# ============================================================ 对话模型

class ChatBrain:
    """对话回复。演示模式用规则，真实模式走 OpenAI 兼容接口。"""

    def __init__(self, engine, demo: bool = True):
        self.engine = engine
        self.demo = demo

    def reply(self, text: str) -> str:
        t = text.strip()
        if not t:
            return "你说什么？我这边没收到内容。"
        recorded = self._recorded_reply(t)
        if recorded is not None:
            return recorded
        if self.demo:
            return self._demo_reply(t)
        return self._model_reply(t)

    def _recorded_reply(self, t: str) -> str | None:
        if any(k in t for k in ("状态", "怎么样", "进度", "跑到哪")):
            tasks = self.engine.store.tasks(1)
            if not tasks:
                return "现在还没有任务。左边提交一个，我跑给你看。"
            task = tasks[0]
            events = self.engine.store.events(task["task_id"])
            last = events[-1] if events else None
            return (f"最近一个任务是 {task['task_id']}，状态 {task['status']}，"
                    f"已经走到第 {task['rounds']} 轮"
                    + (f"，最后一步是 {last['stage']}。" if last else "。"))
        if any(k in t for k in ("驳回", "被打回", "审查意见", "卡在哪", "失败的测试", "为什么失败")):
            tasks = self.engine.store.tasks(1)
            if not tasks:
                return "还没有任务记录。"
            for event in reversed(self.engine.store.events(tasks[0]["task_id"])):
                payload = json.loads(event["payload"] or "{}")
                if event["stage"] == "error":
                    return "运行记录显示：" + str(payload.get("reason") or "执行失败。")
                if event["stage"] == "check-review":
                    if payload.get("ok"):
                        return f"最近任务第 {payload.get('round')} 轮的独立审查已通过。"
                    return "独立审查退回了候选：" + "；".join(payload.get("issues") or ["详见阶段事件。"])
                if event["stage"] == "check-tests" and not payload.get("ok"):
                    failures = [case for case in payload.get("details", []) if not case.get("ok")]
                    evidence = json.dumps(failures[:2], ensure_ascii=False) if failures else payload.get("error")
                    return f"最近任务第 {payload.get('round')} 轮在测试阶段退回。{evidence or ''}"
            return "目前还没有这项任务的测试失败或独立审查记录。"
        return None

    def _demo_reply(self, t: str) -> str:
        if any(k in t for k in ("你好", "hi", "hello", "在吗")):
            return "在。你可以让我跑任务，也可以问我刚才为什么被打回。"
        if any(k in t for k in ("谢谢", "辛苦")):
            return "不客气。要再来一个任务吗？"
        return ("收到。我是这个面板里的助手：你可以提交任务让我跑，"
                "也可以直接问我状态、或者为什么某一轮被驳回。")

    def _model_reply(self, t: str) -> str:
        from .runtime import OpenAICompatibleModel, ModelError
        m = self.engine.model
        if not isinstance(m, OpenAICompatibleModel):
            return "请先配置模型接口。"
        try:
            tasks = self.engine.store.tasks(3)
            ctx = "；".join(f"{x['task_id']}={x['status']}" for x in tasks) or "暂无任务"
            out = m.complete([
                {"role": "system", "content": "你是 be队作品 面板里的助手。回答简短、具体，"
                                              "不要客服腔。可以引用任务状态。"},
                {"role": "user", "content": f"当前任务：{ctx}\n\n用户说：{t}"},
            ], role="chat", contract=None)
            return out.strip()[:500]
        except ModelError as e:
            return f"模型调用失败：{e} 可从顶部「切换模型」进入离线演示。"


# ============================================================ 任务事件 → 插话

def bridge_events(engine, hub: Hub, chat: ChatStore, demo: bool = True):
    """包一层 store.event：任务每走一步，除了落库，还给面板推一条。

    只有"人应该知道"的阶段才插话，避免刷屏。
    """
    original = engine.store.event

    def wrapped(task_id: str, stage: str, payload=None) -> int:
        seq = original(task_id, stage, payload)
        hub.publish({"type": "task", "task_id": task_id, "stage": stage,
                     "seq": seq, "payload": payload or {}})
        text = interjection_for(stage, payload or {}, task_id)
        if text:
            task = engine.store.task(task_id) or {}
            label = str(task.get("title") or task_id)[:60]
            msg = chat.add("assistant", f"「{label}」\n{text}", kind="interject")
            hub.publish({"type": "chat", "message": msg})
        return seq

    engine.store.event = wrapped

    # 任务结束（成功/失败）也要说一句：Engine 用 upsert_task 收尾
    original_upsert = engine.store.upsert_task

    def wrapped_upsert(task_id, digest, status, rounds=0, detail=None, title=None):
        previous = engine.store.task(task_id)
        original_upsert(task_id, digest, status, rounds, detail, title=title)
        if status in ("succeeded", "failed") and (not previous or previous.get("status") != status):
            text = ("任务已完成，可在「执行结果」查看输出和代码。" if status == "succeeded"
                    else "任务未完成。" + (detail or {}).get("reason", "请查看阶段事件。"))
            label = str(title or (previous or {}).get("title") or task_id)[:60]
            msg = chat.add("assistant", f"「{label}」\n{text}", kind="interject")
            hub.publish({"type": "chat", "message": msg})
        hub.publish({"type": "task-update", "task_id": task_id, "status": status, "rounds": rounds})

    engine.store.upsert_task = wrapped_upsert
    return engine


def interjection_for(stage: str, payload: dict, task_id: str) -> str:
    if stage == "check-tests" and payload.get("ok") is False:
        if payload.get("attempted") is False:
            return ""
        if payload.get("mode") == "smoke":
            return f"第 {payload.get('round')} 轮脚本试运行失败，运行错误已反馈给编码角色。"
        failed = payload.get("passed", 0)
        total = payload.get("total", 0)
        return f"第 {payload.get('round')} 轮测试没过（{failed}/{total}），我把失败用例塞回给编码那边了。"
    if stage == "check-review" and payload.get("ok") is False:
        issues = payload.get("issues") or []
        return f"测试过了，但我审查时不同意：{'；'.join(issues)}"
    if stage == "check-static" and payload.get("ok") is False:
        issues = payload.get("issues") or []
        return f"静态检查没过：{'；'.join(issues[:2])}"
    if stage == "cache-stale":
        return "缓存里那条我重跑了一遍，现在的测试不过，已经淘汰。"
    return ""


# ============================================================ 页面

HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>be队作品 · 控制台</title>
<style>
:root{
  --navy:#0f2a5f; --blue:#1d4ed8; --blue-soft:#eaf1fe;
  --ink:#16202e; --muted:#61707f; --line:#e2e9f3; --bg:#f4f7fc;
  --ok:#0a7a3d; --ok-bg:#e7f6ec; --warn:#b45309; --warn-bg:#fff7e6;
  --fail:#b42318; --fail-bg:#fdecea;
}
*{box-sizing:border-box}
body{margin:0;font:14px/1.65 "Segoe UI","微软雅黑",system-ui,sans-serif;color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased}

/* 顶栏 */
.topbar{background:#fff;border-bottom:1px solid var(--line);padding:12px 26px;display:flex;align-items:center;justify-content:space-between;gap:16px;position:sticky;top:0;z-index:20}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#14337a,#1d4ed8);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;letter-spacing:.5px}
.title{font-size:16px;font-weight:700;color:var(--navy);letter-spacing:.3px}
.sub{font-size:12px;color:var(--muted);margin-top:1px}
.top-actions{display:flex;align-items:center;gap:10px}
.pill{font-size:12px;padding:3px 10px;border-radius:999px;background:var(--ok-bg);color:var(--ok);border:1px solid #cfe9d8}

/* 布局 */
main{display:grid;grid-template-columns:1.08fr .92fr;gap:18px;padding:18px 26px 30px;max-width:1560px;margin:0 auto}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px;box-shadow:0 2px 10px rgba(15,42,95,.05)}
.card h2{font-size:14px;margin:0 0 4px;color:var(--navy);display:flex;justify-content:space-between;align-items:center;font-weight:700}
.card h2 small{font-weight:400;color:var(--muted);font-size:12px}
.hint{font-size:12px;color:var(--muted);margin:0 0 12px}
.divider{height:1px;background:var(--line);margin:16px 0 14px}

/* 表单 */
input,textarea,button,select{font:inherit}
input[type=text],input[type=password],textarea{width:100%;border:1px solid var(--line);border-radius:9px;padding:9px 11px;outline:none;background:#fbfdff;transition:border-color .15s}
input[type=text]:focus,input[type=password]:focus,textarea:focus{border-color:#9dbcff;background:#fff}
button{border:0;border-radius:9px;padding:9px 16px;background:var(--blue);color:#fff;cursor:pointer;font-weight:600;white-space:nowrap;transition:filter .15s}
button:hover{filter:brightness(1.06)}
button.ghost{background:var(--blue-soft);color:var(--navy)}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;margin-top:8px}

/* 任务列表 */
.task{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px;background:#fcfdff}
.task .top{display:flex;justify-content:space-between;font-size:12.5px;gap:10px}
.badge{font-size:11.5px;padding:1px 9px;border-radius:999px;background:var(--blue-soft);color:var(--navy);white-space:nowrap}
.badge.ok{background:var(--ok-bg);color:var(--ok)}
.badge.fail{background:var(--fail-bg);color:var(--fail)}

/* 事件 */
.events{min-height:140px;max-height:200px;overflow:auto;font:12px/1.55 Consolas,"Cascadia Mono",monospace;color:#3d4b5c;background:#f8fafd;border:1px solid var(--line);border-radius:9px;padding:9px 11px}

/* 建议 */
.sugg-bar{display:flex;align-items:center;gap:9px;margin:12px 0 6px;flex-wrap:wrap}
.sugg-label{font-size:12px;color:var(--muted);white-space:nowrap}
#suggests{display:flex;gap:7px;flex-wrap:wrap;flex:1}
.chip{border:1px solid #cfe0ff;background:#f4f8ff;color:var(--navy);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer;font-family:inherit;transition:background .15s}
.chip:hover{background:#e8f1ff}
.sugg-stat{font-size:11.5px;color:var(--muted);white-space:nowrap}

/* 决策记录表 */
table.inner{width:100%;border-collapse:collapse;font-size:12px}
.empty{padding:22px 0;text-align:center;color:#9aa9b8;font-size:12.5px;border:1px dashed var(--line);border-radius:9px;background:#fbfdff}
table.inner th{background:var(--blue-soft);color:var(--navy);font-weight:600;text-align:left;padding:6px 8px;border:1px solid var(--line)}
table.inner td{padding:6px 8px;border:1px solid var(--line);vertical-align:top}
table.inner td.mono{font-family:Consolas,monospace;font-size:11px;color:#3d4b5c}

/* 对话 */
#chat{min-height:46vh;max-height:60vh;overflow:auto;padding:6px 2px}
.msg{margin:10px 0;display:flex}
.msg .bub{max-width:84%;padding:9px 13px;border-radius:13px;background:#eef3fc;white-space:pre-wrap;word-break:break-word}
.msg.me{justify-content:flex-end}
.msg.me .bub{background:var(--blue);color:#fff}
.msg.interject .bub{background:var(--warn-bg);border:1px dashed #f0c36d}
.msg .who{font-size:11px;color:var(--muted);margin-bottom:3px}
.sw{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}

/* 弹层 */
.overlay{position:fixed;inset:0;background:rgba(15,42,95,.42);display:flex;align-items:center;justify-content:center;z-index:50;padding:24px}
.overlay.hidden{display:none}
.panel{background:#fff;border-radius:16px;max-width:760px;width:100%;max-height:88vh;overflow:auto;box-shadow:0 20px 60px rgba(10,30,70,.28)}
.panel .ph{padding:22px 26px 14px;border-bottom:1px solid var(--line)}
.panel .ph h3{margin:0;font-size:19px;color:var(--navy)}
.panel .ph p{margin:6px 0 0;color:var(--muted);font-size:13px}
.panel .pb{padding:18px 26px}
.panel .pf{padding:14px 26px 22px;display:flex;gap:10px;justify-content:flex-end;border-top:1px solid var(--line)}
.steps{display:grid;gap:12px}
.step{display:flex;gap:12px;align-items:flex-start}
.step .n{flex:0 0 26px;height:26px;border-radius:8px;background:var(--blue-soft);color:var(--navy);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px}
.step .t b{color:var(--navy)}
.step .t div{font-size:13px;color:var(--muted);margin-top:2px}
.kv{display:grid;grid-template-columns:120px 1fr;gap:8px 14px;font-size:13px;margin-top:6px}
.kv dt{color:var(--muted)}
.kv dd{margin:0;color:var(--ink)}
code{background:#f1f5fb;border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:12px}

@media (max-width:980px){main{grid-template-columns:1fr}.topbar{padding:12px 16px}main{padding:14px 16px 24px}}
@media (max-width:600px){.topbar,.top-actions{flex-wrap:wrap;gap:8px}.top-actions button{padding:6px 10px}}

/* 开始页：白底 + 水流 */
#landing{position:fixed;inset:0;z-index:80;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden}
#landing.hidden{display:none}
.waves{position:absolute;inset:0;overflow:hidden}
.waves span{position:absolute;left:-50%;width:200%;height:42%;bottom:-12%;border-radius:45%;opacity:.5;filter:blur(2px)}
.waves span:nth-child(1){background:linear-gradient(90deg,#dbeafe,#bfdbfe);animation:spin 26s linear infinite}
.waves span:nth-child(2){background:linear-gradient(90deg,#e0f2fe,#c7e6fb);animation:spin 34s linear infinite reverse;bottom:-18%}
.waves span:nth-child(3){background:linear-gradient(90deg,#eef6ff,#dbeafe);animation:spin 46s linear infinite;bottom:-24%}
@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
.landing-inner{position:relative;z-index:2;text-align:center;padding:0 24px;max-width:720px}
.landing-inner h1{font-size:34px;color:#0f2a5f;margin:0 0 10px;letter-spacing:.5px}
.landing-inner .tag{font-size:15px;color:#4b5f75;margin:0 0 6px}
.landing-inner .quote{font-size:14px;color:#61707f;margin:0 0 34px}
.landing-actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.landing-actions button{padding:12px 26px;font-size:15px;border-radius:10px}
.landing-actions .ghost{padding:12px 22px}
.landing-ver{position:absolute;bottom:22px;left:0;right:0;text-align:center;font-size:12px;color:#9aa9b8;z-index:2}

/* 统计条 */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0 4px}
.stat{background:#f8fbff;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:18px;color:var(--navy);line-height:1.2}
.stat span{font-size:11.5px;color:var(--muted)}

/* 模式切换 */
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;overflow:hidden;background:#f6f9fe}
.seg button{padding:5px 14px;border-radius:0;background:transparent;color:var(--muted);font-weight:500;font-size:12.5px}
.seg button.on{background:var(--blue);color:#fff}

/* 思考链路 */
.chain{display:flex;flex-direction:column;gap:0;margin-top:4px}
.node{display:flex;gap:10px;padding:6px 0}
.node .dot{flex:0 0 18px;height:18px;border-radius:50%;background:#e8eefb;color:#4a5c74;font-size:11px;display:flex;align-items:center;justify-content:center;margin-top:2px}
.node.ok .dot{background:var(--ok-bg);color:var(--ok)}
.node.bad .dot{background:var(--fail-bg);color:var(--fail)}
.node .body b{font-size:12.5px;color:var(--navy)}
.node .body div{font-size:12px;color:var(--muted)}
.node .line{flex:0 0 1px;background:var(--line);margin-left:9px}

/* 大输入框 */
#desc{min-height:92px;font-size:14px;line-height:1.7}
.contract-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px}
.contract-tools select{padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:#fff}
.contract-tools button{padding:5px 10px;font-size:12px}
#checks{margin-top:8px;border:1px solid var(--line);border-radius:9px;padding:8px 11px}
#checks summary{color:var(--navy);cursor:pointer}
.case-fields{display:grid;grid-template-columns:1fr 1fr auto;align-items:start;gap:8px;margin:10px 0}
.case-fields label,#nameField{display:grid;gap:3px;font-size:12px;color:var(--muted)}
.case-fields textarea{min-height:45px;resize:vertical;line-height:1.45}
.case-fields .empty-check{display:block}
#nameField[hidden],.empty-check[hidden]{display:none}
#checkHint,#taskNotice{margin:6px 0}
#taskNotice{color:var(--fail);white-space:pre-wrap}
.case-fields button,#addCase{padding:5px 10px;font-size:12px}
#result pre,.test-detail{white-space:pre-wrap;overflow-wrap:anywhere}
#result pre{background:#f8fafd;padding:10px;border-radius:7px;overflow:auto}
.test-detail{margin:6px 0;font:12px/1.55 Consolas,"微软雅黑",monospace}
.task[role=button]{cursor:pointer}
@media(max-width:600px){.case-fields{grid-template-columns:1fr}.case-fields button{justify-self:start}}


/* 回放控制条 */
.replay-bar{display:flex;align-items:center;gap:10px;margin:12px 0 2px;flex-wrap:wrap}
.replay-tip{font-size:12px;color:var(--muted)}
.replay-badge{font-size:12px;padding:3px 11px;border-radius:999px;background:#fff3d6;color:#8a5a00;border:1px solid #f2d59b;font-weight:600}
.replay-badge.hidden{display:none}
.node.hl{background:#fff8e6;border-radius:8px;box-shadow:0 0 0 1px #f0d9a8 inset}
</style></head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="logo">be</div>
    <div>
      <div class="title">be队作品</div>
      <div class="sub">可验证的多 Agent 编排 · 控制台</div>
    </div>
  </div>
  <div class="top-actions">
    <span id="modelMode" class="pill" aria-live="polite">读取运行模式…</span>
    <button id="chooseMode" class="ghost">切换模型</button>
    <button id="helpBtn" class="ghost">使用说明</button>
  </div>
</header>
<main>
  <section class="card">
    <h2>任务 <small id="taskCount">0 个</small></h2>
    <p class="hint" id="taskHint">离线模式可复现下方加法示例。在线模式可编写 Python 脚本或指定函数。</p>
    <textarea id="desc" aria-label="任务说明" placeholder="例如：写一个 Python 脚本，在屏幕上打印 Hello World">实现 add(a,b) 返回两数之和，支持负数与小数</textarea>
    <div class="contract-tools"><label for="taskType">交付形式</label>
      <select id="taskType"><option value="auto">按任务说明识别</option><option value="script">脚本（标准输入 / 输出）</option><option value="function">函数（参数 / 返回值）</option></select>
      <button id="restoreDemo" class="ghost">载入加法示例</button>
    </div>
    <details id="checks"><summary id="checkSummary">验收条件</summary>
      <p class="hint" id="checkHint"></p>
      <label id="nameField">函数名<input type="text" id="fname" autocomplete="off" placeholder="例如 square"></label>
      <div id="cases"></div><button id="addCase" class="ghost">添加用例</button>
    </details>
    <p id="taskNotice" role="status"></p>
    <div class="row" style="align-items:center;justify-content:space-between">
      <div class="seg" id="modeSwitch">
        <button data-mode="auto" class="on">全自动</button>
        <button data-mode="manual">人工确认</button>
      </div>
      <button id="run">开始执行</button>
    </div>
    <div class="stats">
      <div class="stat"><b id="statRounds">—</b><span>平均轮次</span></div>
      <div class="stat"><b id="statCache">—</b><span>缓存命中</span></div>
      <div class="stat"><b id="statSugg">—</b><span>建议命中率</span></div>
      <div class="stat"><b id="statFacts">—</b><span>锁定事实</span></div>
    </div>
    <div class="replay-bar">
      <button id="replayBtn" class="ghost">逐轮回放</button>
      <span class="replay-tip" id="replayTip">按轮次回放所选任务</span>
      <span class="replay-badge hidden" id="replayBadge"></span>
    </div>
    <div class="sugg-bar"><span class="sugg-label">下一步可能是</span><div id="suggests"></div><span id="suggStats" class="sugg-stat"></span></div>
    <div id="tasks"></div>
    <section id="result" hidden><div class="divider"></div><h2>执行结果</h2><div id="resultBody"></div></section>
    <div class="divider"></div>
    <h2>思考链路 <small id="chainMeta">所选任务</small></h2>
    <p class="hint">规划 → 生成 → 静态检查 → 测试 → 审查 → 裁决，一道一道过。</p>
    <div class="chain" id="chain"><div class="empty">提交任务后，这里会显示它每一步在想什么、卡在哪。</div></div>
    <div class="divider"></div>
    <h2>结构化决策记录 <small id="innerMeta">每轮一条</small></h2>
    <p class="hint">每轮一条：事实锁定由代码从契约抽取，避开开头是上一轮用过的，避免重复。</p>
    <div id="innerTable" class="inner-wrap"></div>
    <div class="divider"></div>
    <h2>阶段事件 <small>最新的在下</small></h2>
    <p class="hint">规划、生成、静态检查、测试、审查、裁决的完整流水，落库可回放。</p>
    <div class="events" id="events"></div>
  </section>

  <section class="card">
    <h2>对话 <small><label class="sw"><input type="checkbox" id="interject" checked> 主动插话</label></small></h2>
    <p class="hint">问它「现在到哪一轮了」「为什么被驳回」，它会照实回答；勾选后它会自己汇报进度。</p>
    <div id="chat"></div>
    <div class="row"><input id="msg" type="text" placeholder="问状态 / 问为什么被驳回 / 随便聊"><button id="send">发送</button></div>
  </section>
</main>
<div id="help" class="overlay hidden">
  <div class="panel">
    <div class="ph">
      <h3>使用说明</h3>
      <p>三分钟看懂这个原型在做什么，以及怎么验证它。</p>
    </div>
    <div class="pb">
      <h4 style="margin:0 0 8px;font-size:14px;color:#0f2a5f">界面分区</h4>
      <dl class="kv">
        <dt>任务区</dt><dd>离线入口可复现加法示例。在线时，输入任务说明，选择脚本或函数，在「验收条件」填写输出或返回值。脚本也可以只试运行，由独立审查角色核对需求。</dd>
        <dt>下一步建议</dt><dd>根据上一轮结果给出可点选项，点一下自动提交，也可自己手打</dd>
        <dt>任务列表</dt><dd>每个任务的状态徽章显示「成功 / 失败 / 中断」和轮次</dd>
        <dt>结构化决策记录</dt><dd>每轮的事实锁定、要避开的开头、配图语音开关、上下文用量</dd>
        <dt>阶段事件</dt><dd>规划、生成、静态检查、测试、审查、裁决的完整流水</dd>
        <dt>对话区</dt><dd>可以问状态、问为什么被驳回；勾选「主动插话」后它会自己汇报</dd>
      </dl>
      <div class="divider"></div>
      <h4 style="margin:0 0 8px;font-size:14px;color:#0f2a5f">怎么判断它没在糊弄你</h4>
      <div class="steps">
        <div class="step"><div class="n">A</div><div class="t"><b>看第 2 轮</b>
          <div>可复现离线演示的第 2 轮对加法结果取整，公开测试仍有 3/3 通过。独立审查另测两个小数反例，记录实际输出与期望值。在线模式的轮次取决于模型的实际回答。</div></div></div>
        <div class="step"><div class="n">B</div><div class="t"><b>看决策记录的事实锁定列</b>
          <div>那些数字是从任务契约里用代码抽出来的，不是模型自己说的。</div></div></div>
        <div class="step"><div class="n">C</div><div class="t"><b>看事件流</b>
          <div>每一步都写了库，失败也能回答「卡在哪一轮、为什么」。</div></div></div>
      </div>
      <div class="divider"></div>
      <h4 style="margin:0 0 8px;font-size:14px;color:#0f2a5f">命令行</h4>
      <div style="font-size:13px;color:#3d4b5c;line-height:2">
        <code>python -m tidal_harness demo</code> 跑一遍三轮<br>
        <code>python -m tidal_harness serve --demo</code> 打开本控制台<br>
        <code>python -m tidal_harness run --file 示例任务.json --script fail-first</code> 运行离线示例
      </div>
    </div>
    <div class="pf"><button id="helpClose">知道了</button></div>
  </div>
</div>

<div id="landing">
  <div class="waves"><span></span><span></span><span></span></div>
  <div class="landing-inner">
    <h1>be队作品</h1>
    <p class="tag">可验证的多 Agent 编排</p>
    <p class="quote">Agent 说“做完了”不算数，过了两道独立的门才算数。</p>
    <div class="landing-actions">
      <!-- bundled-mode-button -->
      <button id="enterDemo" class="ghost">可复现离线演示</button>
      <button id="enterKey" class="ghost">用我的 API Key</button>
    </div>
    <p id="modeStatus" role="status"></p>
    <button id="keepMode" class="ghost" hidden>返回控制台</button>
  </div>
  <div class="landing-ver" id="verLine">v1.0 · 测试版</div>
</div>

<div id="apikey" class="overlay hidden">
  <div class="panel" style="max-width:560px">
    <div class="ph">
      <h3>接入你自己的模型</h3>
      <p>填完点保存，任务就会用你的模型跑。不填也能用内置演示模式。</p>
    </div>
    <div class="pb">
      <div class="row"><input id="kBase" type="text" placeholder="接口地址，例如 https://api.deepseek.com/v1"></div>
      <div class="row"><input id="kKey" type="password" autocomplete="off" placeholder="你的 API Key"></div>
      <div class="row"><input id="kModel" type="text" placeholder="模型名，例如 deepseek-flash"></div>
      <p class="hint" style="margin-top:10px">本机服务在内存中使用你的 Key，模型请求会发送到你填写的接口地址。</p>
    </div>
    <div class="pf">
      <button id="keyCancel" class="ghost">取消</button>
      <button id="keySave">保存并使用</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
// ---- 弹层控制 ----
(function(){
  const hp = $('#help');
  $('#helpBtn').onclick = () => hp.classList.remove('hidden');
  $('#helpClose').onclick = () => hp.classList.add('hidden');
  hp.onclick = (e) => { if (e.target === hp) hp.classList.add('hidden'); };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hp.classList.add('hidden'); });
})();
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
const demoDescription = $('#desc').defaultValue;
const demoTests = [[[1, 2], 3], [[-1, 5], 4], [[0.5, 0.5], 1]];
let formType, presetDescription = '', lastInferredName = '', selectedTaskId = '', detailRequest = 0;
function inferTask(description){
  const asksForScript = /(?:写|编写|生成|创建|实现|制作|开发|交付)(?:一?[个段份支]|出)?\s*(?:Python\s*)?(?:控制台|命令行|小)?(?:程序|脚本)|^\s*(?:Python\s*)?(?:脚本|程序)|\b(?:write|create|make|build|implement|generate)\s+(?:(?:a|an)\s+)?(?:(?:standalone|python|console|command[- ]line)\s+)*(?:script|program)\b/i.test(description);
  if (asksForScript) return {type:'script', name:''};
  const signature = description.match(/(?:\bdef\s+|(?:实现|编写|设计|定义|implement|define|function|函数)\s*[\x60'"]*)([A-Za-z_]\w*)\s*[(（]/i)
    || description.match(/(?:函数(?:名(?:为|是)?)?|function\s+(?:called\s+|named\s+)?)[\s\x60'"]*([A-Za-z_]\w*)/i);
  const name = signature ? signature[1] : '';
  const namedFunction = name && !/^(print|input|to|that|which|a|named|called)$/i.test(name);
  const asksForFunction = /(?:实现|编写|创建|定义|写|设计)(?:一[个支段])?(?:\s*Python\s*)?函数|\b(?:implement|write|define|create)\s+(?:a\s+)?(?:python\s+)?function\b/i.test(description);
  return {type:namedFunction || asksForFunction ? 'function' : 'script', name:namedFunction ? name : ''};
}
function displayValue(value){
  if (typeof value !== 'string') return JSON.stringify(value);
  // Keep numeric-looking and multiline strings distinct from numbers and argument separators.
  try { JSON.parse(value); return JSON.stringify(value); } catch (_) {}
  return value.trim() !== value || value.includes('\n') || !value ? JSON.stringify(value) : value;
}
function functionValue(raw){
  try { return JSON.parse(raw); } catch (_) { return raw; }
}
function addCase(args, expected){
  const script = formType === 'script';
  const row = document.createElement('div');
  row.className = 'case-fields';
  row.innerHTML = '<label>' + (script ? '标准输入（可留空）' : '函数参数（每行一个）') +
    '<textarea rows="2" class="case-input"></textarea></label><label>' +
    (script ? '期望标准输出（逐行填写）' : '期望返回值') +
    '<textarea rows="2" class="case-expected"></textarea></label><button type="button" class="ghost" aria-label="移除此用例">移除</button>';
  if (script) {
    const label = document.createElement('label');
    label.className = 'empty-check';
    label.innerHTML = '<input type="checkbox"> 核对空输出';
    row.children[1].append(label);
    label.firstChild.checked = expected === '';
    label.firstChild.onchange = () => {
      if (label.firstChild.checked) row.querySelector('.case-expected').value = '';
      updateSummary();
    };
  }
  const input = row.querySelector('.case-input'), output = row.querySelector('.case-expected');
  if (args) input.value = script ? (args[0] || '') : args.map(displayValue).join('\n');
  if (expected !== undefined) output.value = script ? expected : displayValue(expected);
  input.placeholder = script ? '程序从 input() 读取的文本' : '例如 2 和 3 分两行填写';
  output.placeholder = script ? '例如 Hello World' : '例如 5';
  row.querySelector('button').onclick = () => { row.remove(); updateSummary(); };
  row.oninput = e => {
    if (script && e.target === output && output.value !== '') row.querySelector('input').checked = false;
    updateSummary();
  };
  $('#cases').append(row);
  updateSummary();
}
function updateSummary(){
  const rows = Array.from($('#cases').children);
  const count = rows.filter(row => row.querySelector('.case-expected').value !== '' || row.querySelector('input:checked')).length;
  $('#checkSummary').textContent = '验收条件 · ' + (formType === 'script' ? '脚本' : '函数 ' + $('#fname').value) +
    (count ? ' · ' + count + ' 组用例' : formType === 'script' ? ' · 试运行与独立审查' : ' · 请填写用例');
}
function showContract(type, name, tests){
  formType = type;
  $('#fname').value = name;
  $('#nameField').hidden = type === 'script';
  $('#checkHint').textContent = type === 'script'
    ? '可填写输入和期望输出逐项核对。留空时试运行脚本、展示实际输出，再由审查角色核对任务说明。'
    : '填写参数和期望返回值。文本可直接输入；数字、列表和布尔值按其实际类型比较。';
  $('#cases').replaceChildren();
  tests.forEach(t => {
    if (Array.isArray(t)) addCase(t[0], t[1]);
    else if (type === 'script' && 'stdout' in t) addCase([t.stdin || ''], t.stdout);
    else addCase(t.args, t.expected);
  });
  if (!tests.length) addCase();
  updateSummary();
}
function syncContract(force = false){
  const desc = $('#desc').value.trim(), choice = $('#taskType').value, inferred = inferTask(desc);
  const type = choice === 'auto' ? inferred.type : choice;
  const demo = desc === demoDescription && type === 'function';
  const leftPreset = presetDescription && desc !== presetDescription;
  const newName = type === 'function' && inferred.name && inferred.name !== lastInferredName && inferred.name !== $('#fname').value;
  const removedInferredName = type === 'function' && !inferred.name && lastInferredName && lastInferredName === $('#fname').value;
  if (force || type !== formType || leftPreset || newName || removedInferredName) {
    showContract(type, type === 'function' ? inferred.name : '', demo ? demoTests : []);
    presetDescription = demo ? demoDescription : '';
    $('#checks').open = !demo;
  }
  lastInferredName = inferred.name;
  $('#taskNotice').textContent = '';
}
$('#desc').addEventListener('input', () => syncContract());
$('#taskType').onchange = () => syncContract();
$('#fname').oninput = () => {
  // The former function's examples cannot validate a newly named function.
  presetDescription = '';
  $('#cases').replaceChildren(); addCase(); updateSummary();
};
$('#restoreDemo').onclick = () => {
  $('#desc').value = demoDescription; $('#taskType').value = 'auto'; syncContract(true);
};
$('#addCase').onclick = () => addCase();
syncContract(true);
function taskFromForm(){
  const description = $('#desc').value.trim();
  if (!description) throw new Error('请先写下要完成的任务。');
  const body = {description, task_type:formType, tests:[]};
  if (formType === 'function') {
    body.function_name = $('#fname').value.trim();
    if (!body.function_name) throw new Error('请填写函数名，或把交付形式改为脚本。');
  }
  for (const row of $('#cases').children) {
    const input = row.querySelector('.case-input').value, expected = row.querySelector('.case-expected').value;
    if (formType === 'script') {
      if (expected !== '' || row.querySelector('input').checked) body.tests.push([[input], expected]);
      else if (input !== '') throw new Error('有标准输入的用例还需填写期望输出，或勾选「核对空输出」。');
    } else if (expected !== '') {
      body.tests.push([input ? input.split('\n').map(functionValue) : [], functionValue(expected)]);
    } else if (input !== '') {
      throw new Error('请填写这组函数参数的期望返回值。空字符串可写为 ""。');
    }
  }
  if (formType === 'function' && !body.tests.length) throw new Error('请为这个函数填写至少一组参数和期望返回值。');
  return body;
}
function addMsg(m){
  if (m.kind === 'interject' && !$('#interject').checked) return;
  const el = document.createElement('div');
  el.className = 'msg ' + (m.role === 'user' ? 'me' : '') + (m.kind === 'interject' ? ' interject' : '');
  el.innerHTML = '<div><div class="who">' + (m.role === 'user' ? '我' : (m.kind === 'interject' ? '助手（主动插话）' : '助手')) +
    '</div><div class="bub">' + esc(m.text) + '</div></div>';
  $('#chat').appendChild(el); $('#chat').scrollTop = 1e9;
}
function renderInner(d){
    const rows = (d.events || []).filter(e => e.stage === 'inner');
    if (!rows.length) { $('#innerTable').innerHTML = '<div class="empty">还没有记录。跑一个任务，这里会按轮次显示事实锁定、避开的开头与上下文用量。</div>'; return; }
    let html = '<table class="inner"><tr><th>轮次</th><th>事实锁定</th><th>避开开头</th><th>配图/语音</th><th>上下文</th></tr>';
    rows.forEach(e => {
      let p = {}; try { p = JSON.parse(e.payload || '{}'); } catch (_) {}
      const facts = (p.facts_locked || []).join(', ') || '—';
      const avoid = (p.avoid_openings || []).length;
      const ch = (p.channels && p.channels.image ? '图' : '') + (p.channels && p.channels.voice ? '音' : '') || '—';
      html += '<tr><td>' + esc(p.round) + '</td><td class="mono">' + esc(facts) + '</td><td>' + avoid + ' 条</td><td>' + ch + '</td><td>' + esc(p.context_tokens || 0) + ' tok</td></tr>';
    });
    html += '</table>';
    $('#innerTable').innerHTML = html;
}
async function loadMetrics(){
  try {
    const r = await fetch('/api/metrics'); const d = await r.json();
    const rr = d.repeat || {};
    $('#innerMeta').textContent = '任务 ' + d.tasks + ' · 记录 ' + d.inner + ' 条 · 事实 ' + d.facts + ' 项 · 开头重复率 ' + Math.round((rr.rate || 0) * 100) + '%';
  } catch (e) {}
}
async function loadSuggestions(){
  try {
    const r = await fetch('/api/suggest'); const d = await r.json();
    const box = $('#suggests'); box.innerHTML = '';
    (d.suggestions || []).forEach(s => {
      const b = document.createElement('button');
      b.className = 'chip'; b.textContent = s.label; b.title = s.why || '';
      b.onclick = () => useSuggestion(s);
      box.appendChild(b);
    });
    $('#suggStats').textContent = d.stats ? ('命中 ' + d.stats.accepted + '/' + d.stats.shown) : '';
  } catch (e) { /* 预测失败不影响主流程 */ }
}
async function useSuggestion(s){
  try {
    fetch('/api/suggest/accept', {method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({id: s.id, kind: s.kind})}).then(loadSuggestions);
    if (s.kind === 'task' && s.payload) {
      const p = s.payload;
      $('#desc').value = p.description || '';
      $('#taskType').value = p.task_type || (p.function_name ? 'function' : 'script');
      showContract($('#taskType').value, p.function_name || '', p.tests || []);
      presetDescription = $('#desc').value.trim();
      lastInferredName = inferTask(presetDescription).name;
      $('#taskNotice').textContent = '';
      $('#run').click();
    } else if (s.kind === 'chat' && s.payload) {
      $('#msg').value = s.payload.text || '';
      $('#send').click();
    }
  } catch (e) { alert('执行失败：' + e.message); }
}
async function loadChat(){
  const r = await fetch('/api/chat'); const d = await r.json();
  $('#chat').innerHTML = ''; d.messages.forEach(addMsg);
}
async function loadTasks(){
  const r = await fetch('/api/tasks'); const d = await r.json();
  $('#taskCount').textContent = d.tasks.length + ' 个';
  $('#tasks').innerHTML = d.tasks.map(t => {
    const cls = t.status === 'succeeded' ? 'ok' : (t.status === 'failed' ? 'fail' : '');
    const title = (t.title || '').slice(0, 60) || (t.task_id || '');
    let reason = '';
    if (t.status === 'failed') { try { reason = JSON.parse(t.detail || '{}').reason || ''; } catch (_) {} }
    const status = {queued:'排队中',running:'运行中',succeeded:'成功',failed:'失败',interrupted:'已中断',cancelled:'已取消'}[t.status] || t.status;
    return '<div class="task" role="button" tabindex="0" data-id="' + esc(t.task_id) + '"><div class="top"><b>' + esc(title) +
      '</b><span class="badge ' + cls + '">' + esc(status) + ' · ' + esc(t.rounds) + ' 轮</span></div>' +
      (reason ? '<div>' + esc(reason) + '</div>' : '') + '</div>';
  }).join('') || '<div style="color:#5b6b7a;font-size:12.5px">还没有任务</div>';
}
function showTask(id){
  if (!id) return;
  selectedTaskId = id;
  loadEvents(id);
}
$('#tasks').onclick = e => showTask(e.target.closest('[data-id]')?.dataset.id);
$('#tasks').onkeydown = e => {
  if (e.key === 'Enter' || e.key === ' ') {
    const task = e.target.closest('[data-id]');
    if (task) { e.preventDefault(); showTask(task.dataset.id); }
  }
};
function testSummary(p){
  if (p.attempted === false) return '静态检查未通过，本轮未执行测试';
  if (p.mode === 'smoke') return (p.ok ? '试运行完成' : '试运行失败') + '（无输出断言）';
  return (p.mode === 'stdout' ? '标准输出 ' : '') + p.passed + '/' + p.total + (p.ok ? ' 通过' : ' 未通过');
}
function testEvidence(p){
  const lines = [];
  (p.details || []).forEach((test, n) => {
    if (test.kind === 'smoke' || p.mode === 'smoke') {
      lines.push('实际输出\n' + (test.stdout || '（空）'));
    } else if (p.mode === 'stdout') {
      lines.push('用例 ' + (n+1) + (test.ok ? ' ✓' : ' ×') + '\n标准输入\n' + (test.stdin || '（空）') +
        '\n期望输出\n' + (test.expected || '（空）') + '\n实际输出\n' + (test.stdout || '（空）'));
    } else {
      lines.push('用例 ' + (n+1) + (test.ok ? ' ✓' : ' ×') +
        (Array.isArray(test.args) ? '　参数 ' + JSON.stringify(test.args) : '') +
        (Object.hasOwn(test, 'expected') ? '　期望返回 ' + test.expected : '') +
        (Object.hasOwn(test, 'got') ? '　实际返回 ' + test.got : ''));
    }
    if (test.error) lines.push(test.error);
  });
  if (p.error) lines.push(p.error);
  return lines.join('\n');
}
function renderResult(d){
  const checks = (d.events || []).filter(e => e.stage === 'check-tests' || e.stage === 'cache-hit');
  const event = checks[checks.length-1];
  const last = event ? JSON.parse(event.payload) : null;
  const detail = JSON.parse(d.task.detail || '{}');
  const code = detail.code || (last && last.candidate);
  $('#result').hidden = !(last || code || detail.reason);
  const body = $('#resultBody');
  body.replaceChildren();
  function block(tag, text) {
    const el = document.createElement(tag);
    el.textContent = text; body.append(el); return el;
  }
  if (last) {
    block('b', event.stage === 'cache-hit' ? '复用候选 · 当前用例重验 ' + last.tests : '第 ' + last.round + ' 轮 · ' + testSummary(last));
    const evidence = testEvidence(last);
    if (evidence) block('pre', evidence);
  }
  if (detail.reason) block('p', detail.reason);
  if (last && event.stage === 'check-tests') {
    const reviews = d.events.filter(e => e.stage === 'check-review');
    const review = reviews.length ? JSON.parse(reviews[reviews.length-1].payload) : null;
    if (review && review.round === last.round) {
      block('p', review.ok ? '独立审查通过' : '独立审查退回：' + (review.issues || []).join('；'));
    }
  }
  if (code) {
    block('b', '候选代码');
    const pre = block('pre', code), copy = block('button', '复制代码');
    copy.className = 'ghost';
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(code); copy.textContent = '已复制'; }
      catch (_) {
        const range = document.createRange(); range.selectNodeContents(pre);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        copy.textContent = '代码已选中，请复制';
      }
    };
  }
}
async function loadEvents(id){
  if (!id) return;
  const request = ++detailRequest;
  const r = await fetch('/api/tasks/' + encodeURIComponent(id)); const d = await r.json();
  if (request !== detailRequest || id !== selectedTaskId || !d.task) return;
  $('#chainMeta').textContent = (d.task.title || id).slice(0, 40);
  renderResult(d); renderInner(d); window.renderChain(d);
  const html = (d.events || []).map(e =>
    '<div>#' + e.seq + ' ' + esc(e.stage) + ' ' + esc((e.payload || '').slice(0, 120)) + '</div>').join('');
  $('#events').innerHTML = html || '<div class="empty">还没有事件。提交一个任务后，这里会显示规划、生成、检查、审查的完整流水。</div>';
}
$('#run').onclick = async () => {
  $('#run').disabled = true;
  $('#taskNotice').textContent = '';
  try {
    const body = taskFromForm();
    const r = await fetch('/api/tasks', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '提交失败，请重试。');
    showTask(d.task_id);
  } catch (e) {
    $('#checks').open = true;
    $('#taskNotice').textContent = e.message;
  } finally { $('#run').disabled = false; setTimeout(loadTasks, 300); setTimeout(loadSuggestions, 800); setTimeout(loadMetrics, 1200); }
};
$('#send').onclick = async () => {
  const t = $('#msg').value.trim(); if (!t) return;
  $('#msg').value = '';
  try {
    const r = await fetch('/api/chat', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({text:t})});
    if (!r.ok) throw new Error((await r.json()).error || '发送失败');
  } catch (e) { $('#msg').value = t; alert(e.message); }
};
$('#msg').addEventListener('keydown', e => { if (e.key === 'Enter') $('#send').click(); });
$('#interject').onchange = loadChat;
const es = new EventSource('/api/stream');
function displayMode(d) {
  const label = {offline:'可复现离线', deepseek:'DeepSeek 在线', custom:'自定义在线'}[d.mode];
  if (!label) return;
  $('#modelMode').textContent = label + (d.mode === 'offline' ? '' : ' · ' + d.model);
  $('#taskHint').textContent = d.mode === 'offline'
    ? '离线模式只复现预设加法示例。自定义脚本和函数请切换到 DeepSeek 在线或自己的模型。'
    : '写下 Python 任务。脚本可核对标准输出，函数可核对给定参数的返回值。';
  if (d.mode !== 'offline') $('#keepMode').hidden = false;
}
async function readMode() {
  try {
    const r = await fetch('/api/mode');
    if (r.ok) displayMode(await r.json());
  } catch (_) { $('#modelMode').textContent = '服务连接中断'; }
}
es.onopen = readMode;
let taskRefreshTimer;
es.onmessage = ev => {
  const d = JSON.parse(ev.data);
  if (d.type === 'mode') readMode();
  if (d.type === 'chat') addMsg(d.message);
  if (d.type === 'task' || d.type === 'task-update') {
    if (!selectedTaskId) selectedTaskId = d.task_id;
    if (!taskRefreshTimer) taskRefreshTimer = setTimeout(() => {
      taskRefreshTimer = null;
      loadTasks(); loadSuggestions(); loadMetrics(); loadEvents(selectedTaskId);
    }, 120);
  }
};
loadChat(); loadTasks(); loadSuggestions(); loadMetrics();
fetch('/api/tasks').then(r=>r.json()).then(d => { if (!selectedTaskId && d.tasks[0]) showTask(d.tasks[0].task_id); });

// ================= 新版交互 =================
(function(){
  let manual = false;
  const $id = (x) => document.getElementById(x);

  // 开始页
  const land = $id('landing');
  readMode();
  $id('chooseMode').onclick = () => {
    $id('modeStatus').textContent = '';
    $id('keepMode').hidden = false;
    land.classList.remove('hidden');
  };
  $id('keepMode').onclick = () => land.classList.add('hidden');
  async function enterMode(mode) {
    const controls = land.querySelectorAll('button');
    controls.forEach(b => b.disabled = true);
    $id('modeStatus').textContent = '正在切换…';
    try {
      const r = await fetch('/api/mode', {method:'POST', headers:{'content-type':'application/json'},
        body:JSON.stringify({mode})});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || '切换失败，请重试。');
      displayMode(d); land.classList.add('hidden');
      $id('modeStatus').textContent = '';
    } catch (e) { $id('modeStatus').textContent = e.message; }
    finally { controls.forEach(b => b.disabled = false); }
  }
  $id('enterDemo').onclick = () => enterMode('offline');
  if ($id('enterDeepSeek')) $id('enterDeepSeek').onclick = () => enterMode('deepseek');
  $id('enterKey').onclick = () => { land.classList.add('hidden'); $id('apikey').classList.remove('hidden'); };
  $id('keyCancel').onclick = () => {
    $id('kKey').value = ''; $id('apikey').classList.add('hidden'); land.classList.remove('hidden');
  };
  $id('keySave').onclick = async () => {
    const base = $id('kBase').value.trim(), key = $id('kKey').value.trim(), model = $id('kModel').value.trim();
    if (!base || !key) { alert('接口地址和 Key 都要填'); return; }
    try {
      const r = await fetch('/api/config', {method:'POST', headers:{'content-type':'application/json'},
        body: JSON.stringify({base, key, model})});
      const d = await r.json();
      if (r.ok) { $id('kKey').value = ''; displayMode(d); $id('apikey').classList.add('hidden'); }
      else alert('保存失败：' + (d.error || '未知错误'));
    } catch (e) { alert('保存失败：' + e.message); }
  };

  // 模式切换
  document.querySelectorAll('#modeSwitch button').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('#modeSwitch button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      manual = b.dataset.mode === 'manual';
      appendSys(manual ? '已切换为「人工确认」：每次执行前我会先问你。' : '已切换为「全自动」：我会直接把任务跑完。');
    };
  });
  function appendSys(text){
    const chat = $id('chat');
    const div = document.createElement('div');
    div.className = 'msg';
    div.innerHTML = '<div><div class="who">系统</div><div class="bub">' + text.replace(/[<>&]/g, '') + '</div></div>';
    chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
  }

  // 执行前确认（人工模式）
  const origRun = $id('run').onclick;
  $id('run').onclick = async () => {
    const desc = ($id('desc').value || '').trim().slice(0, 60);
    if (manual && !confirm('人工确认：现在开始执行这个任务吗？\n\n' + desc)) return;
    return origRun();
  };
  const origUse = window.useSuggestion;
  window.useSuggestion = async (s) => {
    if (manual && !confirm('人工确认：要执行「' + s.label + '」吗？')) return;
    return origUse(s);
  };

  // 统计条
  async function refreshStats(){
    try {
      const d = await (await fetch('/api/metrics')).json();
      $id('statRounds').textContent = (d.avgRounds != null ? d.avgRounds : '—');
      $id('statCache').textContent = (d.cacheHitRate != null ? Math.round(d.cacheHitRate * 100) + '%' : '—');
      const st = d.suggest || {};
      $id('statSugg').textContent = st.shown ? (Math.round((st.rate || 0) * 100) + '%') : '—';
      $id('statFacts').textContent = (d.facts != null ? d.facts : '—');
    } catch (e) {}
  }
  setInterval(refreshStats, 4000); refreshStats();

  // 思考链路
  const LABEL = {
    queued:'任务入队', plan:'规划：拆步骤与验收标准', do:'生成候选代码',
    inner:'记录本轮约束', 'check-static':'静态检查（AST 白名单）',
    'check-tests':'确定性测试', 'check-review':'独立审查', act:'裁决', error:'执行失败'
  };
  function nodeClass(e){
    const p = safe(e.payload);
    if (e.stage === 'error') return 'bad';
    if (e.stage === 'act') return p.result === 'accepted' ? 'ok' : 'bad';
    if (e.stage === 'check-tests' || e.stage === 'check-static' || e.stage === 'check-review')
      return p.ok === false ? 'bad' : (p.ok === true ? 'ok' : '');
    return '';
  }
  function nodeText(e){
    const p = safe(e.payload);
    if (e.stage === 'error') return p.reason || '请查看阶段事件。';
    if (e.stage === 'check-tests') return testSummary(p);
    if (e.stage === 'check-review') {
      const checks = p.checks || [];
      const tally = checks.length ? ' · 附加用例 ' + checks.filter(c => c.ok).length + '/' + checks.length : '';
      return (p.ok ? '审查通过' : '驳回') + tally + (p.ok ? '' : '：' + (p.issues || []).join('；'));
    }
    if (e.stage === 'check-static') return p.ok ? '通过' : ('问题：' + ((p.issues || [])[0] || ''));
    if (e.stage === 'act') return p.result === 'accepted' ? '接受，写入缓存' : '打回，带意见重来';
    if (e.stage === 'do') return '第 ' + (p.round || '?') + ' 轮';
    if (e.stage === 'inner') return '锁定 ' + ((p.facts_locked || []).length) + ' 项事实';
    return '';
  }
  function safe(x){ try { return JSON.parse(x || '{}'); } catch (e) { return {}; } }
  window.renderChain = function(d){
      const rows = (d.events || []).filter(e => LABEL[e.stage]);
      const box = $id('chain');
      if (!rows.length) { box.innerHTML = '<div class="empty">提交任务后，这里会显示它每一步在想什么、卡在哪。</div>'; return; }
      box.innerHTML = rows.map(e => {
        const cls = nodeClass(e), t = nodeText(e);
        return '<div class="node ' + cls + '" data-round="' + Number(safe(e.payload).round || 0) + '"><div class="dot">' + (cls === 'ok' ? '✓' : cls === 'bad' ? '!' : '·') + '</div>' +
          '<div class="body"><b>' + LABEL[e.stage] + '</b>' + (t ? '<div>' + esc(t) + '</div>' : '') +
          (e.stage === 'check-tests' ? '<details><summary>查看用例和实际结果</summary><pre class="test-detail">' +
            esc(testEvidence(safe(e.payload))) + '</pre></details>' : '') + '</div></div>';
      }).join('');
  };
})();

  // ---- 逐轮回放：每轮停顿，自动往下走 ----
  let replayTimer = null;
  const parseJ = (x) => { try { return JSON.parse(x || '{}'); } catch (_) { return {}; } };
  async function replayRounds(){
    if (replayTimer) { clearInterval(replayTimer); replayTimer = null; }
    const badge = document.getElementById('replayBadge');
    const tip = document.getElementById('replayTip');
    if (!selectedTaskId) { tip.textContent = '还没有任务，先点「开始执行」'; return; }
    const d = await (await fetch('/api/tasks/' + encodeURIComponent(selectedTaskId))).json();
    const evs = d.events || [];
    const only = evs.filter(e => /check-tests|check-review|do/.test(e.stage));
    const rounds = [...new Set(only.map(e => parseJ(e.payload).round).filter(x => x > 0))].sort((a,b)=>a-b);
    if (!rounds.length) { tip.textContent = '这个任务没有可回放的轮次'; return; }
    const nodes = [...document.querySelectorAll('#chain .node')];
    let i = 0;
    const step = () => {
      nodes.forEach(n => n.classList.remove('hl'));
      const r = rounds[i];
      badge.textContent = '第 ' + r + ' 轮 / 共 ' + rounds.length + ' 轮';
      badge.classList.remove('hidden');
      const seg = nodes.filter(n => Number(n.dataset.round) === r);
      seg.forEach(n => n.classList.add('hl'));
      if (seg[0]) seg[0].scrollIntoView({block:'center', behavior:'smooth'});
      tip.textContent = (i === rounds.length - 1) ? '最后一轮：' + r : '正在回放第 ' + r + ' 轮，下一轮自动开始';
      i++;
      if (i >= rounds.length) {
        clearInterval(replayTimer); replayTimer = null;
        tip.textContent = '回放结束：共 ' + rounds.length + ' 轮';
      }
    };
    replayTimer = setInterval(step, 2200);
    step();
  }
  document.getElementById('replayBtn').onclick = replayRounds;

</script></body></html>"""


# ============================================================ HTTP

def make_server(engine, hub: Hub, chat: ChatStore, brain: ChatBrain,
                host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    from .contract import TaskContract
    from .engine import TaskScheduler
    from .runtime import ModelError, OpenAICompatibleModel, ScriptedDemoModel

    scheduler = TaskScheduler(engine, workers=2)
    bundled_key = Path(__file__).resolve().parent.parent / "deepseek-competition.key"
    mode_lock = threading.Lock()
    mode = "offline" if brain.demo else "custom"
    cache_scope = uuid.uuid4().hex
    active_chats = 0

    def mode_state():
        return {"mode": mode, "model": "scripted" if mode == "offline" else engine.model.model}

    def bundled_available():
        return ipaddress.ip_address(server.server_address[0]).is_loopback and bundled_key.is_file()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-frame-options", "DENY")
            self.send_header("x-content-type-options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _valid_host(self):
            # Loopback and explicitly bound addresses only; a browser with a rebound DNS name
            # must never gain same-origin access to a locally bound competition credential.
            try:
                authority = urlsplit("http://" + self.headers.get("Host", ""))
                if (authority.port or 80) != self.server.server_port or authority.username is not None:
                    return False
                dest = self.connection.getsockname()[0]
                return authority.hostname in {dest, host.lower()} or (
                    authority.hostname == "localhost" and ipaddress.ip_address(dest).is_loopback)
            except (ValueError, OSError):
                return False

        def _same_origin(self):
            if self.headers.get("Sec-Fetch-Site", "none") not in {"same-origin", "none"}:
                return False
            origin = self.headers.get("Origin")
            expected = "http://" + self.headers.get("Host", "")
            if origin is not None:
                return origin.lower() == expected.lower()
            referer = self.headers.get("Referer")
            if referer:
                try:
                    url = urlsplit(referer)
                    return (url.scheme + "://" + url.netloc).lower() == expected.lower()
                except ValueError:
                    return False
            return True

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if not self._valid_host():
                return self._json({"error": "请通过服务绑定的本机地址访问。"}, 403)
            if path in ("/", "/index.html"):
                button = '<button id="enterDeepSeek">一键 DeepSeek 在线</button>' if bundled_available() else ""
                page = HTML.replace("<!-- bundled-mode-button -->", button)
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/mode":
                with mode_lock:
                    state = mode_state()
                return self._json(state)
            if path == "/api/chat":
                return self._json({"messages": chat.recent(200)})
            if path == "/api/tasks":
                return self._json({"tasks": engine.store.tasks(50)})
            if path.startswith("/api/tasks/"):
                tid = path.rsplit("/", 1)[-1]
                task = engine.store.task(tid)
                if not task:
                    return self._json({"error": "not found"}, 404)
                return self._json({"task": task, "events": engine.store.events(tid)})
            if path == "/api/metrics":
                from .inner import repeat_rate
                tasks = engine.store.tasks(50)
                samples = []
                for t in tasks:
                    detail = t.get("detail")
                    if detail:
                        try:
                            code = json.loads(detail).get("code") or ""
                            if code.strip():
                                samples.append(code.strip().splitlines()[0])
                        except Exception:
                            pass
                rr = repeat_rate(samples)
                inner_count = 0
                facts = 0
                for t in tasks[:10]:
                    for e in engine.store.events(t["task_id"]):
                        if e["stage"] == "inner":
                            inner_count += 1
                            try:
                                facts += len((json.loads(e["payload"] or "{}")).get("facts_locked") or [])
                            except Exception:
                                pass
                cache_hits = 0
                rounds = []
                for t in tasks:
                    rounds.append(t.get("rounds") or 0)
                    detail = t.get("detail") or ""
                    if "fromCache" in str(detail):
                        cache_hits += 1
                avg_rounds = round(sum(rounds) / len(rounds), 1) if rounds else 0
                return self._json({
                    "repeat": rr, "tasks": len(tasks), "inner": inner_count, "facts": facts,
                    "avgRounds": avg_rounds,
                    "cacheHitRate": (cache_hits / len(tasks)) if tasks else 0.0,
                    "suggest": chat.suggestion_stats(),
                })
            if path == "/api/suggest":
                tasks = engine.store.tasks(1)
                last = tasks[0] if tasks else None
                state = {
                    "last_task": last,
                    "last_events": engine.store.events(last["task_id"]) if last else [],
                }
                items = [s.to_json() for s in predict.suggest_next(state)]
                if self._same_origin():
                    for s in items:
                        chat.note_suggestion(s["id"], s["kind"], False)
                return self._json({"suggestions": items, "stats": chat.suggestion_stats()})
            if path == "/api/stream":
                return self._stream()
            return self._json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            nonlocal active_chats
            path = self.path.split("?")[0]
            if not self._valid_host() or not self._same_origin():
                self.close_connection = True
                return self._json({"error": "仅接受来自本控制台的请求。"}, 403)
            try:
                length = int(self.headers.get("content-length", 0))
                if not 0 <= length <= 1024 * 1024:
                    raise ValueError
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, UnicodeError):
                self.close_connection = True
                return self._json({"error": "请提供有效的 JSON 对象（最大 1 MiB）。"}, 400)
            if path in ("/api/mode", "/api/config"):
                with mode_lock:
                    response, status = self._set_mode(path, data)
                return self._json(response, status)
            if path == "/api/chat":
                text = str(data.get("text", ""))[:1000]
                if not text.strip():
                    return self._json({"error": "empty"}, 400)
                with mode_lock:
                    active_chats += 1
                try:
                    reply = self._chat(text)
                finally:
                    with mode_lock:
                        active_chats -= 1
                return self._json({"message": reply})
            if path == "/api/suggest/accept":
                sid = str(data.get("id", ""))[:64]
                if sid:
                    chat.note_suggestion(sid, str(data.get("kind", "task")), True)
                return self._json({"ok": True, "stats": chat.suggestion_stats()})
            if path == "/api/tasks":
                try:
                    c = TaskContract.from_data(data, task_id=data.get("task_id") or f"http-{uuid.uuid4().hex[:16]}")
                    priority = data.get("priority", 5)
                    if type(priority) is not int or not 0 <= priority <= 9:
                        raise ValueError("priority 必须是 0 至 9 的整数")
                    with mode_lock:
                        if mode == "offline" and not c.is_add_demo:
                            raise ValueError("离线模式只复现预设加法。请点顶部「切换模型」，选择 DeepSeek 在线或自己的模型，再执行当前任务。")
                        scope = cache_scope if mode == "custom" else mode
                        c = replace(c, tenant=scope, model=mode_state()["model"])
                        scheduler.submit(c, priority=priority, use_cache=mode == "custom")
                except FileExistsError as e:
                    return self._json({"error": str(e)}, 409)
                except queue.Full:
                    return self._json({"error": "等待队列已满，请稍后重试"}, 429)
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 400)
                return self._json({"task_id": c.task_id, "status": "queued"}, 202)
            return self._json({"error": "not found"}, 404)

        def _chat(self, text):
            msg = chat.add("user", text)
            hub.publish({"type": "chat", "message": msg})
            msg = chat.add("assistant", brain.reply(text))
            hub.publish({"type": "chat", "message": msg})
            return msg

        def _set_mode(self, path, data):
            nonlocal mode, cache_scope
            requested = "custom" if path == "/api/config" else data.get("mode")
            if path == "/api/mode":
                if set(data) != {"mode"} or requested not in ("offline", "deepseek"):
                    return {"error": "请选择可复现离线或 DeepSeek 在线模式。"}, 400
                if requested == "deepseek" and not bundled_available():
                    return {"error": "本机未启用参赛在线入口，可选择离线或使用自己的 Key。"}, 403
                if requested == mode:
                    return mode_state(), 200
            # submit() and changes to the model share mode_lock. unfinished_tasks covers
            # waiting jobs as well as jobs already taken by either worker.
            with scheduler.q.mutex:
                busy = scheduler.q.unfinished_tasks != 0
            if busy or active_chats:
                return {"error": "任务或对话正在进行，请等它结束后再切换。"}, 409
            if requested == "offline":
                adapter = ScriptedDemoModel("fail-first")
            elif requested == "deepseek":
                try:
                    key = bundled_key.read_text(encoding="utf-8-sig").strip()
                    if not key or any(ch.isspace() for ch in key):
                        raise ValueError
                    adapter = OpenAICompatibleModel("https://api.deepseek.com", key, "deepseek-flash")
                except (OSError, ValueError, ModelError):
                    return {"error": "本机参赛凭据不可用，可选择离线或使用自己的 Key。"}, 503
            else:
                if not all(isinstance(data.get(k, ""), str) for k in ("base", "key", "model")):
                    return {"error": "接口地址、Key 和模型名须为文本。"}, 400
                base, key = (data.get(k, "").strip() for k in ("base", "key"))
                model = data.get("model", "").strip() or "deepseek-chat"
                if not base or not key:
                    return {"error": "请填写接口地址和自己的 Key。"}, 400
                try:
                    adapter = OpenAICompatibleModel(base, key, model)
                except (ValueError, ModelError):
                    return {"error": "请检查接口地址、Key 和模型名。"}, 400
                cache_scope = uuid.uuid4().hex
            engine.model = adapter
            brain.demo = requested == "offline"
            mode = requested
            hub.publish({"type": "mode"})
            return mode_state(), 200

        def _stream(self):
            q = hub.subscribe()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-store")
            self.send_header("connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=15)
                        data = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                        self.wfile.write(data.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

        def log_message(self, *a):
            return

    server = ThreadingHTTPServer((host, port), H)
    scheduler.start()
    return server
