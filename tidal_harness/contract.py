"""任务契约 + Agent 注册表 + 规划器。

契约是整套系统的"单一事实来源"：排队前校验，执行中保留，缓存隔离用它做键。
"""

from __future__ import annotations

import hashlib
import json
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
    function_name: str          # 必须实现的函数名
    tests: tuple[TestCase, ...]  # 用户给定的测试
    max_rounds: int = 3
    context_budget_tokens: int = 3000
    deadline_seconds: float = 120.0
    tenant: str = "default"
    model: str = "demo"

    # ---------- 校验 ----------
    def validate(self) -> None:
        if not self.description.strip():
            raise ContractError("description 不能为空")
        if not self.function_name.isidentifier():
            raise ContractError(f"函数名不合法：{self.function_name!r}")
        if not self.tests:
            raise ContractError("至少要有一个测试用例")
        if not (1 <= self.max_rounds <= 5):
            raise ContractError("max_rounds 必须在 1~5 之间")
        if self.context_budget_tokens < 256:
            raise ContractError("context_budget_tokens 太小（<256）")
        if self.deadline_seconds <= 0:
            raise ContractError("deadline_seconds 必须为正")
        for t in self.tests:
            if not isinstance(t.args, tuple):
                raise ContractError("测试参数必须是 tuple")

    # ---------- 身份 ----------
    def digest(self) -> str:
        """契约摘要：改一个字就变，用它做缓存隔离与版本守卫的依据。"""
        payload = json.dumps(
            {
                "description": self.description,
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
            instruction="实现给定函数，只输出 JSON：{\"code\": \"...\", \"explanation\": \"...\"}",
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
