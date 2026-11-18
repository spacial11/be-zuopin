"""浏览器面板：直接和助手对话 + 主动插话 + 任务与事件。

落点说明：参赛版不依赖 QQ、不依赖任何外部机器人框架，
所有交互都在这个页面里完成：
  · 左边：提交任务、看阶段事件（排队/规划/生成/静态/测试/审查/接受）
  · 右边：和助手对话；任务有动静时它会自己插一句
零依赖：http.server + Server-Sent Events + 原生 JS，不引任何前端库。
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time

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
        self.conn.execute("INSERT INTO suggestions(id,kind,at,accepted) VALUES(?,?,?,?)",
                          (sid, kind, time.time(), 1 if accepted else 0))
        self.conn.commit()

    def suggestion_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(accepted),0) AS a FROM suggestions").fetchone()
        shown, acc = int(row["n"]), int(row["a"])
        return {"shown": shown, "accepted": acc,
                "rate": round(acc / shown, 3) if shown else 0.0}

    def add(self, role: str, text: str, kind: str = "chat") -> dict:
        at = time.time()
        cur = self.conn.execute("INSERT INTO chat(role,kind,text,at) VALUES(?,?,?,?)",
                                (role, kind, text, at))
        self.conn.commit()
        return {"id": cur.lastrowid, "role": role, "kind": kind, "text": text, "at": at}

    def recent(self, limit: int = 60) -> list[dict]:
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
        if self.demo:
            return self._demo_reply(t)
        return self._model_reply(t)

    def _demo_reply(self, t: str) -> str:
        lower = t.lower()
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
        if any(k in t for k in ("为什么", "驳回", "审查")):
            events = self.engine.store.tasks(1)
            if events:
                evs = self.engine.store.events(events[0]["task_id"])
                for e in reversed(evs):
                    if e["stage"] == "check-review":
                        payload = json.loads(e["payload"] or "{}")
                        if payload.get("ok") is False:
                            return "审查那一轮我给了驳回：" + "；".join(payload.get("issues") or [])
                        return "最近这轮审查是通过的，没提问题。"
            return "还没跑过需要审查的任务。"
        if any(k in t for k in ("你好", "hi", "hello", "在吗")):
            return "在。你可以让我跑任务，也可以问我刚才为什么被打回。"
        if any(k in t for k in ("谢谢", "辛苦")):
            return "不客气。要再来一个任务吗？"
        return ("收到。我是这个面板里的助手：你可以提交任务让我跑，"
                "也可以直接问我状态、或者为什么某一轮被驳回。")

    def _model_reply(self, t: str) -> str:
        from .runtime import OpenAICompatibleModel, ModelError
        import os
        base = os.environ.get("TIDAL_MODEL_BASE", "")
        key = os.environ.get("TIDAL_MODEL_KEY", "")
        model = os.environ.get("TIDAL_MODEL_NAME", "deepseek-chat")
        if not base:
            return "（真实模式未配置：请设置 TIDAL_MODEL_BASE / TIDAL_MODEL_KEY）"
        try:
            m = OpenAICompatibleModel(base, key, model)
            tasks = self.engine.store.tasks(3)
            ctx = "；".join(f"{x['task_id']}={x['status']}" for x in tasks) or "暂无任务"
            out = m.complete([
                {"role": "system", "content": "你是 Hope of Light 面板里的助手。回答简短、具体，"
                                              "不要客服腔。可以引用任务状态。"},
                {"role": "user", "content": f"当前任务：{ctx}\n\n用户说：{t}"},
            ], role="reviewer", contract=None)
            return out.strip()[:500]
        except ModelError as e:
            return f"模型调用失败：{e}"


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
            msg = chat.add("assistant", text, kind="interject")
            hub.publish({"type": "chat", "message": msg})
        return seq

    engine.store.event = wrapped

    # 任务结束（成功/失败）也要说一句：Engine 用 upsert_task 收尾
    original_upsert = engine.store.upsert_task

    def wrapped_upsert(task_id, digest, status, rounds=0, detail=None):
        previous = engine.store.task(task_id)
        original_upsert(task_id, digest, status, rounds, detail)
        if status in ("succeeded", "failed") and (not previous or previous.get("status") != status):
            text = ("搞定了：两道门都过，结果已经写进缓存。" if status == "succeeded"
                    else f"任务失败：{rounds} 轮都没过，我把原因记下来了。")
            msg = chat.add("assistant", text, kind="interject")
            hub.publish({"type": "chat", "message": msg})
        hub.publish({"type": "task-update", "task_id": task_id, "status": status, "rounds": rounds})

    engine.store.upsert_task = wrapped_upsert
    return engine


def interjection_for(stage: str, payload: dict, task_id: str) -> str:
    if stage == "check-tests" and payload.get("ok") is False:
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
<title>Hope of Light · 控制台</title>
<style>
:root{--blue:#1d4ed8;--dark:#122e7a;--ink:#1a1a1a;--muted:#5b6b7a;--line:#dde6f2;--bg:#f5f8fd}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 "Segoe UI","微软雅黑",system-ui,sans-serif;color:var(--ink);background:var(--bg)}
header{background:linear-gradient(90deg,#14337a,#1d4ed8);color:#fff;padding:14px 22px;display:flex;align-items:center;gap:14px}
header b{font-size:17px;letter-spacing:.5px}
header span{opacity:.75;font-size:12.5px}
main{display:grid;grid-template-columns:1.05fr .95fr;gap:16px;padding:16px 22px 24px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:0 1px 2px rgba(20,51,122,.04)}
.card h2{font-size:14px;margin:0 0 10px;color:var(--dark);display:flex;justify-content:space-between;align-items:center}
.card h2 small{font-weight:400;color:var(--muted)}
input,textarea,button{font:inherit}
input[type=text],textarea{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;outline:none;background:#fbfdff}
textarea{resize:vertical;min-height:60px}
button{border:0;border-radius:8px;padding:8px 14px;background:var(--blue);color:#fff;cursor:pointer}
button.ghost{background:#eaf1fe;color:var(--dark)}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:8px;margin-top:8px}
.task{border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-bottom:8px}
.task .top{display:flex;justify-content:space-between;font-size:12.5px}
.badge{font-size:11.5px;padding:1px 8px;border-radius:20px;background:#eaf1fe;color:var(--dark)}
.badge.ok{background:#e7f6ec;color:#0a7a3d}
.badge.fail{background:#fdecea;color:#b42318}
.events{max-height:180px;overflow:auto;font:12px/1.5 Consolas,monospace;color:#445;background:#fbfdff;border:1px dashed var(--line);border-radius:8px;padding:8px}
.sugg-bar{display:flex;align-items:center;gap:8px;margin:10px 0 4px;flex-wrap:wrap}
.sugg-label{font-size:12px;color:var(--muted);white-space:nowrap}
#suggests{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.chip{border:1px solid var(--line);background:#f4f8ff;color:var(--dark);border-radius:999px;padding:5px 11px;font-size:12.5px;cursor:pointer;font-family:inherit}
.chip:hover{background:#e8f1ff}
.sugg-stat{font-size:11.5px;color:var(--muted);white-space:nowrap}
.inner-wrap{max-height:190px;overflow:auto}
table.inner{width:100%;border-collapse:collapse;font-size:12px}
table.inner th{background:#eaf1fe;color:var(--dark);font-weight:600;text-align:left;padding:4px 6px;border:1px solid var(--line)}
table.inner td{padding:4px 6px;border:1px solid var(--line);vertical-align:top}
table.inner td.mono{font-family:Consolas,monospace;font-size:11px;color:#445}
#chat{height:52vh;overflow:auto;padding:4px 2px}
.msg{margin:8px 0;display:flex}
.msg .bub{max-width:84%;padding:8px 12px;border-radius:12px;background:#eef3fc;white-space:pre-wrap;word-break:break-word}
.msg.me{justify-content:flex-end}
.msg.me .bub{background:var(--blue);color:#fff}
.msg.interject .bub{background:#fff7e6;border:1px dashed #f0c36d}
.msg .who{font-size:11px;color:var(--muted);margin-bottom:2px}
.sw{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
@media (max-width:900px){main{grid-template-columns:1fr}}
</style></head>
<body>
<header><b>Hope of Light</b><span>可验证的多 Agent 编排 · 控制台</span></header>
<main>
  <section class="card">
    <h2>任务 <small id="taskCount">0 个</small></h2>
    <div class="row"><input id="desc" type="text" placeholder="任务描述，例如：实现 add(a,b) 返回两数之和，支持负数与小数"></div>
    <div class="row"><input id="fname" type="text" value="add" style="max-width:140px">
      <input id="tests" type="text" style="flex:1" value='[[[1,2],3],[[-1,5],4],[[0.5,0.5],1]]'>
      <button id="run">提交</button></div>
    <div class="sugg-bar"><span class="sugg-label">下一步可能是</span><div id="suggests"></div><span id="suggStats" class="sugg-stat"></span></div>
    <div id="tasks"></div>
    <h2 style="margin-top:14px">结构化决策记录 <small id="innerMeta">每轮一条</small></h2>
    <div id="innerTable" class="inner-wrap"></div>
    <h2 style="margin-top:14px">阶段事件 <small>最新的在下</small></h2>
    <div class="events" id="events"></div>
  </section>

  <section class="card">
    <h2>对话 <small><label class="sw"><input type="checkbox" id="interject" checked> 主动插话</label></small></h2>
    <div id="chat"></div>
    <div class="row"><input id="msg" type="text" placeholder="问状态 / 问为什么被驳回 / 随便聊"><button id="send">发送</button></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function addMsg(m){
  if (m.kind === 'interject' && !$('#interject').checked) return;
  const el = document.createElement('div');
  el.className = 'msg ' + (m.role === 'user' ? 'me' : '') + (m.kind === 'interject' ? ' interject' : '');
  el.innerHTML = '<div><div class="who">' + (m.role === 'user' ? '我' : (m.kind === 'interject' ? 'Hope（主动插话）' : 'Hope')) +
    '</div><div class="bub">' + esc(m.text) + '</div></div>';
  $('#chat').appendChild(el); $('#chat').scrollTop = 1e9;
}
async function loadInner(id){
  if (!id) return;
  try {
    const r = await fetch('/api/tasks/' + encodeURIComponent(id));
    const d = await r.json();
    const rows = (d.events || []).filter(e => e.stage === 'inner');
    if (!rows.length) { $('#innerTable').innerHTML = '<div style="color:#5b6b7a;font-size:12px">还没有记录</div>'; return; }
    let html = '<table class="inner"><tr><th>轮次</th><th>事实锁定</th><th>避开开头</th><th>配图/语音</th><th>上下文</th></tr>';
    rows.forEach(e => {
      let p = {}; try { p = JSON.parse(e.payload || '{}'); } catch (_) {}
      const facts = (p.facts_locked || []).join(', ') || '—';
      const avoid = (p.avoid_openings || []).length;
      const ch = (p.channels && p.channels.image ? '图' : '') + (p.channels && p.channels.voice ? '音' : '') || '—';
      html += '<tr><td>' + p.round + '</td><td class="mono">' + facts + '</td><td>' + avoid + ' 条</td><td>' + ch + '</td><td>' + (p.context_tokens || 0) + ' tok</td></tr>';
    });
    html += '</table>';
    $('#innerTable').innerHTML = html;
  } catch (e) { /* 不影响主流程 */ }
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
      if (s.payload.description) $('#desc').value = s.payload.description;
      if (s.payload.function_name) $('#fname').value = s.payload.function_name;
      if (s.payload.tests) $('#tests').value = JSON.stringify(s.payload.tests);
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
    return '<div class="task" data-id="' + t.task_id + '"><div class="top"><b>' + esc(t.task_id) +
      '</b><span class="badge ' + cls + '">' + esc(t.status) + ' · ' + t.rounds + ' 轮</span></div></div>';
  }).join('') || '<div style="color:#5b6b7a;font-size:12.5px">还没有任务</div>';
}
async function loadEvents(id){
  if (!id) return;
  const r = await fetch('/api/tasks/' + encodeURIComponent(id)); const d = await r.json();
  $('#events').innerHTML = (d.events || []).map(e =>
    '<div>#' + e.seq + ' ' + esc(e.stage) + ' ' + esc((e.payload || '').slice(0, 120)) + '</div>').join('');
}
$('#run').onclick = async () => {
  const body = {description: $('#desc').value || '实现 add(a,b)',
    function_name: $('#fname').value || 'add', tests: JSON.parse($('#tests').value)};
  $('#run').disabled = true;
  try {
    const r = await fetch('/api/tasks', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) alert('提交失败：' + (await r.text()));
    else { const d = await r.json(); setTimeout(() => loadEvents(d.task_id), 400); }
  } finally { $('#run').disabled = false; setTimeout(loadTasks, 300); setTimeout(loadSuggestions, 800); setTimeout(function(){ loadInner(0); loadMetrics(); }, 1200); }
};
$('#send').onclick = async () => {
  const t = $('#msg').value.trim(); if (!t) return;
  $('#msg').value = ''; addMsg({role:'user', kind:'chat', text:t});
  const r = await fetch('/api/chat', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({text:t})});
  const d = await r.json(); if (d.message) addMsg(d.message);
};
$('#msg').addEventListener('keydown', e => { if (e.key === 'Enter') $('#send').click(); });
$('#interject').onchange = loadChat;
const es = new EventSource('/api/stream');
es.onmessage = ev => {
  const d = JSON.parse(ev.data);
  if (d.type === 'chat') addMsg(d.message);
  if (d.type === 'task' || d.type === 'task-update') { loadTasks(); loadEvents(d.task_id); loadSuggestions(); loadInner(d.task_id); loadMetrics(); }
};
loadChat(); loadTasks(); loadSuggestions(); loadMetrics(); fetch('/api/tasks').then(r=>r.json()).then(d => { if (d.tasks[0]) { loadEvents(d.tasks[0].task_id); loadInner(d.tasks[0].task_id); } });
</script></body></html>"""


# ============================================================ HTTP

def make_server(engine, hub: Hub, chat: ChatStore, brain: ChatBrain,
                host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    from .contract import TaskContract, TestCase

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _body(self) -> dict:
            n = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw or b"{}")
            except Exception:
                return {}

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/chat":
                return self._json({"messages": chat.recent()})
            if path == "/api/tasks":
                return self._json({"tasks": engine.store.tasks()})
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
                return self._json({"repeat": rr, "tasks": len(tasks), "inner": inner_count, "facts": facts})
            if path == "/api/suggest":
                tasks = engine.store.tasks(1)
                last = tasks[0] if tasks else None
                state = {
                    "last_task": last,
                    "last_events": engine.store.events(last["task_id"]) if last else [],
                }
                items = [s.to_json() for s in predict.suggest_next(state)]
                for s in items:
                    chat.note_suggestion(s["id"], s["kind"], False)
                return self._json({"suggestions": items, "stats": chat.suggestion_stats()})
            if path == "/api/stream":
                return self._stream()
            return self._json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/api/chat":
                data = self._body()
                text = str(data.get("text", ""))[:1000]
                if not text.strip():
                    return self._json({"error": "empty"}, 400)
                user_msg = chat.add("user", text)
                hub.publish({"type": "chat", "message": user_msg})
                reply_text = brain.reply(text)
                bot_msg = chat.add("assistant", reply_text)
                hub.publish({"type": "chat", "message": bot_msg})
                return self._json({"message": bot_msg})
            if path == "/api/suggest/accept":
                data = self._body()
                sid = str(data.get("id", ""))[:64]
                if sid:
                    chat.note_suggestion(sid, str(data.get("kind", "task")), True)
                return self._json({"ok": True, "stats": chat.suggestion_stats()})
            if path == "/api/tasks":
                data = self._body()
                try:
                    def to_case(t):
                        # 支持两种写法：{"args":[...],"expected":...} 或 [[...], expected]
                        if isinstance(t, dict):
                            return TestCase(tuple(t["args"]), t["expected"])
                        return TestCase(tuple(t[0]), t[1])

                    tests = tuple(to_case(t) for t in data["tests"])
                    c = TaskContract(
                        task_id=data.get("task_id") or f"http-{int(time.time()*1000)}",
                        description=str(data["description"]),
                        function_name=str(data["function_name"]),
                        tests=tests,
                        max_rounds=int(data.get("max_rounds", 3)),
                        context_budget_tokens=int(data.get("context_budget_tokens", 2000)),
                    )
                    c.validate()
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": str(e)}, 400)
                threading.Thread(target=lambda: engine.run(c), daemon=True).start()
                return self._json({"task_id": c.task_id, "status": "queued"}, 202)
            return self._json({"error": "not found"}, 404)

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

    return ThreadingHTTPServer((host, port), H)
