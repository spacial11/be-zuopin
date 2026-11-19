"""下一步意图预测。

为什么要做：让用户不必每次手打需求。系统根据**当前状态**（上一个任务结果、
最近的失败原因、对话历史）预测接下来最可能想做什么，给 3 个可点的选项；
用户仍然可以自己输入。

设计约束（和整套系统一致）：
  · 预测必须**可解释**：每个建议都带 why，说明是根据哪条状态推出来的；
  · 预测错了没有代价：不点就行，不阻塞手输；
  · 不猜隐私：只用任务状态与事件，不读记忆、不读人设。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Suggestion:
    """一个可执行的下一步建议。"""

    id: str                 # 稳定标识，用于统计接受率
    label: str              # 按钮上显示的字
    kind: str               # task: 填任务表单 | chat: 发一句话 | info: 只看不改
    payload: dict = field(default_factory=dict)
    why: str = ""           # 为什么推这个（可解释）

    def to_json(self) -> dict:
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "payload": self.payload, "why": self.why,
        }


def _payload(event: dict | None) -> dict:
    """事件里的 payload 可能是 dict，也可能是 sqlite 里读出来的 JSON 字符串。"""
    p = (event or {}).get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p) or {}
        except Exception:
            return {}
    return p or {}


def _last_task(state: dict | None) -> dict | None:
    return (state or {}).get("last_task")


def _last_events(state: dict | None) -> list[dict]:
    return (state or {}).get("last_events") or []


def _review_issues(events: list[dict]) -> list[str]:
    """只看**最近一次**审查的结论——通过就是通过，不再往前翻旧账。"""
    for e in reversed(events):
        if e.get("stage") == "check-review":
            payload = _payload(e)
            if payload.get("ok") is False:
                return list(payload.get("issues") or [])
            return []
    return []


def _failed_tests(events: list[dict]) -> bool:
    for e in reversed(events):
        if e.get("stage") == "check-tests":
            return _payload(e).get("ok") is False
    return False


def suggest_next(state: dict | None = None, limit: int = 3) -> list[Suggestion]:
    """根据状态给出建议。纯函数：同样的状态给同样的建议，便于测试。"""
    task = _last_task(state)
    events = _last_events(state)
    out: list[Suggestion] = []

    # ---------- 冷启动：还没跑过任务 ----------
    if not task:
        out.append(Suggestion(
            id="run-demo-task", label="跑一遍示例任务（加法 + 三条测试）",
            kind="task",
            payload={
                "description": "实现一个函数 add(a, b)，返回两数之和；要求支持负数与小数。",
                "function_name": "add",
                "tests": [[[1, 2], 3], [[-1, 5], 4], [[0.5, 0.5], 1.0]],
            },
            why="还没有任务记录，先用示例把链路跑通",
        ))
        out.append(Suggestion(
            id="ask-what-it-does", label="它到底在验证什么？",
            kind="chat", payload={"text": "这套系统到底在验证什么？"},
            why="新用户通常先问这个",
        ))
        return out[:limit]

    status = task.get("status")
    issues = _review_issues(events)
    plan = next((_payload(e) for e in events if e.get("stage") == "plan"), {})
    if not plan.get("is_add_demo"):
        if issues:
            out.append(Suggestion("review-why", "查看本次审查意见", "chat",
                                  {"text": "这次任务为什么被驳回？"},
                                  "最近一次审查提出了修改意见"))
        out.append(Suggestion("task-status", "查看当前任务状态", "chat",
                              {"text": "这个任务现在是什么状态？"},
                              "根据当前任务记录查看进展"))
        return out[:limit]

    # ---------- 上一个任务失败：优先给"修"的路径 ----------
    if status == "failed":
        out.append(Suggestion(
            id="rerun-with-easier-tests", label="放宽测试再跑一次（先确认链路）",
            kind="task",
            payload={
                "description": "实现一个函数 add(a, b)，返回两数之和（先只要求正数）。",
                "function_name": "add",
                "tests": [[[1, 2], 3], [[3, 4], 7]],
            },
            why="上一轮失败，先用更简单的契约确认链路本身没问题",
        ))
        out.append(Suggestion(
            id="ask-why-failed", label="看看它卡在哪一轮",
            kind="chat", payload={"text": "上个任务卡在哪一轮？为什么？"},
            why="失败任务的第一反应通常是看原因",
        ))

    # ---------- 审查驳回：最典型的一步 ----------
    if issues:
        out.append(Suggestion(
            id="act-on-review", label="按审查意见改一版",
            kind="task",
            payload={
                "description": "实现一个函数 add(a, b)，返回两数之和；显式处理 int 与 float 混合输入。",
                "function_name": "add",
                "tests": [[[1, 2], 3], [[-1, 5], 4], [[0.5, 0.5], 1.0], [[1, 0.5], 1.5]],
            },
            why="审查提了：" + "；".join(issues[:1]),
        ))

    # ---------- 测试挂了 ----------
    if _failed_tests(events):
        out.append(Suggestion(
            id="see-failed-cases", label="看失败的测试用例",
            kind="chat", payload={"text": "失败的测试用例是什么？"},
            why="上一轮确定性测试没通过",
        ))

    # ---------- 成功：给"加深"的路径 ----------
    if status == "succeeded":
        out.append(Suggestion(
            id="add-edge-cases", label="加两个边界用例再验一次",
            kind="task",
            payload={
                "description": "实现一个函数 add(a, b)，返回两数之和；要求支持负数、小数与零。",
                "function_name": "add",
                "tests": [[[1, 2], 3], [[-1, 5], 4], [[0.5, 0.5], 1.0], [[0, 0], 0], [[-0.5, -0.5], -1.0]],
            },
            why="上一轮两道门都过了，下一步通常是加强测试而不是换任务",
        ))
        out.append(Suggestion(
            id="switch-function", label="换个函数试试（乘法）",
            kind="task",
            payload={
                "description": "实现一个函数 mul(a, b)，返回两数之积；要求支持负数与小数。",
                "function_name": "mul",
                "tests": [[[2, 3], 6], [[-2, 3], -6], [[0.5, 4], 2.0]],
            },
            why="同一套流程换一个函数，验证它不是只对加法有效",
        ))

    # ---------- 兜底：永远给一个能立刻做的 ----------
    if not out:
        out.append(Suggestion(
            id="ask-status", label="现在是什么状态？",
            kind="chat", payload={"text": "现在是什么状态？"},
            why="没有更强的信号时，先看状态",
        ))
    return out[:limit]


def acceptance_rate(shown: int, accepted: int) -> float:
    """建议命中率：点了就是接受。这个数字可以进面板，用来判断预测准不准。"""
    if shown <= 0:
        return 0.0
    return round(accepted / shown, 3)
