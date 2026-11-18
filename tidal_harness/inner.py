"""结构化决策记录（把「模型自己写的内心」变成可渲染的字段）。

为什么这么做：一段自然语言内心，模型写糊了你就什么都看不到；
固定字段能让面板直接渲染成表格，也能统计「事实锁定几条」「避重命中几次」。

字段与私有版一致，但含义按竞赛版的边界调整：
  · facts_locked —— **代码侧**从任务契约里抽出来的数字/日期，不是模型自述；
  · avoid_openings —— 最近被用过的开头，用来硬性避开；
  · channels —— 本轮是否用了图/语音（竞赛版恒为 False，保留字段是为了对齐）；
  · injected_mood —— 竞赛版没有情绪系统，恒为空；这不是遗漏，是边界（见白皮书 §6）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

NUM_RE = re.compile(r"(?<!\d)(\d{1,4}(?:[.,]\d+)?)(?!\d)")
DATE_RE = re.compile(r"(?<!\d)\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")


@dataclass
class RoundInner:
    round: int
    facts_locked: list[str] = field(default_factory=list)
    avoid_openings: list[str] = field(default_factory=list)
    channels: dict = field(default_factory=lambda: {"image": False, "voice": False})
    injected_mood: list[str] = field(default_factory=list)
    context_tokens: int = 0
    omitted: list[str] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def facts_from_contract(contract) -> list[str]:
    """从任务契约里抽「不可变事实」：函数名、期望值、测试参数里的数字与日期。

    只抽取、不推断——抽不出来就是空列表，不编。
    """
    found: list[str] = []
    text = " ".join([
        getattr(contract, "description", "") or "",
        getattr(contract, "function_name", "") or "",
    ])
    for t in getattr(contract, "tests", ()) or ():
        text += " " + repr(t.args) + " " + repr(t.expected)

    for m in DATE_RE.findall(text):
        found.append(m)
    for m in NUM_RE.findall(text):
        if any(m in d for d in found):
            continue
        found.append(m)
    # 去重保序，最多 12 条，避免表格被数字刷满
    seen, out = set(), []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out[:12]


def opening(text: str, n: int = 6) -> str:
    s = re.sub(r"^[\s「『\"'（(【\[]+", "", str(text or ""))
    s = re.sub(r"[\s,，。.!！?？…~～]+", "", s)
    return s[: max(1, n)]


def repeat_rate(samples: list[str], n: int = 6) -> dict:
    """开头重复率：同一开头出现多次即算重复。这是可量化的「重复」指标。"""
    items = [s for s in samples if s]
    if len(items) < 2:
        return {"total": len(items), "dup": 0, "rate": 0.0}
    seen, dup = set(), 0
    for s in items:
        k = opening(s, n)
        if k in seen:
            dup += 1
        else:
            seen.add(k)
    return {"total": len(items), "dup": dup, "rate": round(dup / len(items), 3)}
