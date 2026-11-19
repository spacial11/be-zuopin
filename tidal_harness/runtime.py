"""运行时组件：上下文窗口、候选缓存、SQLite 存储、模型适配。

三者都属于"容易被忽略但会出事"的地方：
  · 上下文 —— 长任务里必需材料被挤掉，模型就会忘掉最初的要求；
  · 缓存 —— 跨任务复用却没重新验证，等于把别人的答案当成自己的；
  · 存储 —— 页面刷新/进程重启后，状态必须还在，不能靠内存里那份。
"""

from __future__ import annotations

import json
import re
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable


# ============================================================ 上下文

class ContextOverflow(RuntimeError):
    """必需材料放不进预算 —— 明确失败，而不是悄悄截断。"""


def estimate_tokens(text: str) -> int:
    """工程估计：中文按 1.6 字/token，英文按 4 字符/token，取折中值。

    这不是分词器，只用于预算控制；报告里必须说明它是估计值。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.6 + other / 4) + 1


def _shrink_json(obj: Any, budget_tokens: int, path: str = "") -> tuple[Any, list[str]]:
    """结构化裁剪：/code 原样保留，explanation 允许缩短，其余按需丢弃。

    返回 (裁剪后的对象, 被省略/缩短的路径列表)。
    """
    omitted: list[str] = []
    if estimate_tokens(json.dumps(obj, ensure_ascii=False)) <= budget_tokens:
        return obj, omitted

    if isinstance(obj, dict):
        out = {}
        # 第一轮：保留 code / 必需键
        for k, v in obj.items():
            p = f"{path}/{k}"
            if k in ("code", "function_name", "tests", "task"):
                out[k] = v
                continue
        # 第二轮：按剩余预算塞其它键，长的先砍
        rest = [(k, v) for k, v in obj.items() if k not in out]
        rest.sort(key=lambda kv: -len(json.dumps(kv[1], ensure_ascii=False)))
        for k, v in rest:
            p = f"{path}/{k}"
            trial = dict(out)
            trial[k] = v
            if estimate_tokens(json.dumps(trial, ensure_ascii=False)) <= budget_tokens:
                out[k] = v
            elif isinstance(v, str) and k in ("explanation", "reason", "comment"):
                # 允许缩短的文本：二分找一个能放下的长度
                lo, hi = 0, len(v)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    trial[k] = v[:mid] + "…"
                    if estimate_tokens(json.dumps(trial, ensure_ascii=False)) <= budget_tokens:
                        lo = mid
                    else:
                        hi = mid - 1
                out[k] = v[:lo] + "…"
                omitted.append(p + f" (truncated to {lo} chars)")
            else:
                omitted.append(p + " (dropped)")
        return out, omitted

    if isinstance(obj, list):
        kept = []
        for i, v in enumerate(obj):
            trial = kept + [v]
            if estimate_tokens(json.dumps(trial, ensure_ascii=False)) <= budget_tokens:
                kept.append(v)
            else:
                omitted.append(f"{path}[{i}] (dropped)")
        return kept, omitted

    return obj, omitted


@dataclass
class BuiltContext:
    messages: list[dict]
    used_tokens: int
    omitted: list[str]
    note: str = ""

    def to_json(self) -> dict:
        return {
            "messages": self.messages,
            "used_tokens": self.used_tokens,
            "omitted": self.omitted,
            "note": self.note,
        }


class ContextWindow:
    """滑动窗口：固定保留 + 最近历史优先 + 结构化裁剪。

    固定保留（永远不裁）：系统规则、原始任务、当前输入。
    其余：历史从新到旧塞，塞不下就停；工具结果整块保留，不切一半。
    """

    def __init__(self, budget_tokens: int):
        self.budget = budget_tokens

    def build(
        self,
        *,
        system: str,
        task: str,
        current: str,
        history: Iterable[dict] | None = None,
        tool_blocks: Iterable[dict] | None = None,
        payload: Any | None = None,
    ) -> BuiltContext:
        fixed = [
            {"role": "system", "content": system},
            {"role": "user", "content": "【原始任务·固定保留】\n" + task},
        ]
        used = sum(estimate_tokens(m["content"]) for m in fixed)
        if used > self.budget:
            raise ContextOverflow(
                f"固定材料已超预算：{used} > {self.budget} tokens"
            )

        tail: list[dict] = [{"role": "user", "content": "【当前输入】\n" + current}]
        tail_tokens = estimate_tokens(tail[0]["content"])

        # 工具结果整块保留（宁可少放历史，也不切半块）
        for blk in (tool_blocks or []):
            text = "【工具结果】\n" + json.dumps(blk, ensure_ascii=False)
            t = estimate_tokens(text)
            if used + tail_tokens + t > self.budget:
                raise ContextOverflow("工具结果放不进预算（整块保留，不切半块）")
            tail.append({"role": "user", "content": text})
            tail_tokens += t

        # 历史从新到旧
        kept_hist: list[dict] = []
        omitted: list[str] = []
        acc = used + tail_tokens
        for item in reversed(list(history or [])):
            t = estimate_tokens(item.get("content", ""))
            if acc + t <= self.budget:
                kept_hist.append(item)
                acc += t
            else:
                omitted.append(f"history#{item.get('seq', '?')} (dropped)")

        messages = fixed + list(reversed(kept_hist)) + tail

        note = ""
        if payload is not None:
            room = max(64, self.budget - acc)
            payload, more = _shrink_json(payload, room)
            omitted.extend(more)
            text = "【结构化数据】\n" + json.dumps(payload, ensure_ascii=False)
            messages.append({"role": "user", "content": text})
            acc += estimate_tokens(text)
            if more:
                note = "结构化数据经过裁剪，见 omitted"

        if acc > self.budget:
            raise ContextOverflow(f"构建后仍超预算：{acc} > {self.budget}")
        return BuiltContext(messages=messages, used_tokens=acc, omitted=omitted, note=note)


# ============================================================ 缓存

@dataclass
class CacheEntry:
    key: str
    code: str
    explanation: str
    created_at: float
    hits: int = 0


class CandidateCache:
    """候选缓存：只在**两道检查都通过**之后才允许写入。

    命中后不能直接用 —— 必须用当前测试重新跑一遍（测试契约可能已经变了）。
    """

    def __init__(self, ttl_seconds: float = 3600, capacity: int = 128):
        self.ttl = ttl_seconds
        self.capacity = capacity
        self._data: dict[str, CacheEntry] = {}
        self.stats = {"hit": 0, "miss": 0, "stale": 0}

    @staticmethod
    def key_for(contract_digest: str, model: str, tenant: str) -> str:
        import hashlib
        raw = f"{contract_digest}|{model}|{tenant}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> CacheEntry | None:
        e = self._data.get(key)
        if not e:
            self.stats["miss"] += 1
            return None
        if time.time() - e.created_at > self.ttl:
            self._data.pop(key, None)
            self.stats["stale"] += 1
            return None
        return e

    def put(self, key: str, code: str, explanation: str) -> None:
        if len(self._data) >= self.capacity:
            oldest = min(self._data.values(), key=lambda x: x.created_at)
            self._data.pop(oldest.key, None)
        self._data[key] = CacheEntry(key=key, code=code, explanation=explanation, created_at=time.time())

    def bump(self, key: str) -> None:
        e = self._data.get(key)
        if e:
            e.hits += 1
            self.stats["hit"] += 1

    def evict(self, key: str) -> None:
        self._data.pop(key, None)


# ============================================================ 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  digest TEXT NOT NULL,
  status TEXT NOT NULL,
  rounds INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  detail TEXT,
  title TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  stage TEXT NOT NULL,
  at REAL NOT NULL,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
CREATE TABLE IF NOT EXISTS candidates (
  cache_key TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  explanation TEXT,
  created_at REAL NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0
);
"""


# ---------------- 落盘前脱敏 ----------------
# 原则：在写进 SQLite 之前就把敏感值抹掉，而不是展示时再遮挡。
# 这样即使有人直接打开 data/harness.db，也看不到凭据。

MASK = "***"
_SECRET_KEYS = ("key", "token", "secret", "password", "passwd", "authorization",
                "cookie", "credential", "session")
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"\b[0-9a-f]{32,}\b"),
)


def _scrub_text(value: str) -> str:
    out = value
    for pat in _SECRET_PATTERNS:
        out = pat.sub(MASK, out)
    return out


def _redact(obj: Any, depth: int = 0) -> Any:
    """递归抹掉敏感字段与疑似凭据。深度上限防止自引用。"""
    if depth > 8:
        return MASK
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            key = str(k).lower()
            looks_secret = any(t in key for t in _SECRET_KEYS) and not key.endswith("tokens")
            if looks_secret and isinstance(v, str) and v.strip():
                clean[k] = MASK
            else:
                clean[k] = _redact(v, depth + 1)
        return clean
    if isinstance(obj, (list, tuple)):
        return [_redact(v, depth + 1) for v in obj]
    if isinstance(obj, str):
        return _scrub_text(obj)
    return obj


class Store:
    """SQLite 落盘。页面的唯一事实来源是它，不是内存。"""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "title" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT")
            self.conn.commit()
        self.conn.commit()
        self._seq: dict[str, int] = {}

    # ---- 任务 ----
    def upsert_task(self, task_id: str, digest: str, status: str, rounds: int = 0, detail: Any = None, title: str | None = None) -> None:
        now = time.time()
        with self._db_lock:
            self.conn.execute(
                """INSERT INTO tasks(task_id,digest,status,rounds,created_at,updated_at,detail,title)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,
                     rounds=excluded.rounds, updated_at=excluded.updated_at, detail=excluded.detail,
                     title=COALESCE(excluded.title, tasks.title)""",
                (task_id, digest, status, rounds, now, now,
                 json.dumps(_redact(detail), ensure_ascii=False) if detail is not None else None, title),
            )
            self.conn.commit()

    def task(self, task_id: str) -> dict | None:
        with self._db_lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, limit: int = 50) -> list[dict]:
        with self._db_lock:
            rows = self.conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 事件 ----
    def event(self, task_id: str, stage: str, payload: Any = None) -> int:
        with self._db_lock:
            seq = self._next_seq(task_id)
            self.conn.execute(
                "INSERT INTO events(task_id,seq,stage,at,payload) VALUES(?,?,?,?,?)",
                (task_id, seq, stage, time.time(),
                 json.dumps(_redact(payload), ensure_ascii=False) if payload is not None else None),
            )
            self.conn.commit()
        return seq

    def _next_seq(self, task_id: str) -> int:
        if task_id not in self._seq:
            row = self.conn.execute("SELECT MAX(seq) AS m FROM events WHERE task_id=?", (task_id,)).fetchone()
            self._seq[task_id] = (row["m"] or 0) + 1
            return self._seq[task_id]
        self._seq[task_id] += 1
        return self._seq[task_id]

    def events(self, task_id: str) -> list[dict]:
        with self._db_lock:
            rows = self.conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted(self) -> int:
        """进程重启时调用：把上次没跑完的任务明确标成中断，而不是假装成功。"""
        cur = self.conn.execute(
            "UPDATE tasks SET status='interrupted', updated_at=? WHERE status IN ('queued','running')",
            (time.time(),),
        )
        self.conn.commit()
        return cur.rowcount


# ============================================================ 模型

class ModelError(RuntimeError):
    pass


class DemoModel:
    """离线演示模型：不联网、不花钱，行为确定，用于跑通链路。"""

    def __init__(self):
        self.calls = 0

    def complete(self, messages: list[dict], *, role: str, contract: Any) -> str:
        self.calls += 1
        if role == "planner":
            return json.dumps({"coder": "coder-a", "reviewer": "reviewer-b", "order": ["test", "review"]})
        if role == "coder":
            return json.dumps({
                "code": _demo_code(contract.function_name),
                "explanation": "演示模式：按函数名生成最朴素的实现。",
            })
        return json.dumps({"approve": True, "issues": []})


def _demo_code(function_name: str) -> str:
    if function_name in ("add", "sum"):
        return "def add(a, b):\n    return a + b\n"
    return (
        f"def {function_name}(*args):\n"
        "    total = 0\n"
        "    for a in args:\n"
        "        total += a\n"
        "    return total\n"
    )


class ScriptedDemoModel:
    """离线候选生成器。fail-first 依次演示运算错误、小数截断和修正。"""

    def __init__(self, script: str = "fail-first"):
        self.script = script
        self.coder_calls = 0

    def complete(self, messages: list[dict], *, role: str, contract) -> str:
        if role == "planner":
            return json.dumps({"coder": "coder-a", "reviewer": "reviewer-b", "order": ["test", "review"]})
        if role == "coder":
            self.coder_calls += 1
            if self.script == "fail-first" and self.coder_calls == 1:
                # 第一轮：把加法写成减法 —— 测试必然不过
                code = f"def {contract.function_name}(a, b):\n    return a - b\n"
                return json.dumps({"code": code, "explanation": "先按直觉写一版。"}, ensure_ascii=False)
            if self.script == "fail-first" and self.coder_calls == 2 and contract.function_name == "add":
                code = "def add(a, b):\n    return int(a + b)\n"
                return json.dumps({"code": code, "explanation": "改为相加，但把和转换成了整数。"}, ensure_ascii=False)
            code = f"def {contract.function_name}(a, b):\n    return a + b\n"
            return json.dumps({"code": code, "explanation": "直接返回相加结果，保留小数。"}, ensure_ascii=False)
        return json.dumps({"approve": False, "issues": ["离线审查规则只覆盖示例 add。其他任务请连接模型审查。"]},
                          ensure_ascii=False)

class OpenAICompatibleModel:
    """显式的 OpenAI 兼容适配器。凭据只从参数/环境变量来，不从别的客户端里翻。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict], *, role: str, contract: Any) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.2 if role != "coder" else 0.5,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        if self.api_key:
            req.add_header("authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ModelError(f"模型 HTTP {e.code}: {e.read()[:200]!r}") from e
        except Exception as e:  # noqa: BLE001
            raise ModelError(f"模型调用失败：{e}") from e
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            raise ModelError(f"响应结构不对：{data!r}") from e
