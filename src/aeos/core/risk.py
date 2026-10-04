"""Deterministic risk policy engine.

The policy is declarative YAML (policy/risk_policy.yaml), unit-tested, and never LLM-controlled.
Decisions are keyed on the risk level DECLARED by the tool's server, not on tool names alone, so
renaming/aliasing a tool cannot bypass it.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, Field

from aeos.core.state import RiskDecision

DEFAULT_POLICY_PATH = Path("policy/risk_policy.yaml")
_STRICTNESS = {"allow": 0, "require_approval": 1, "deny": 2}


class ApprovalRequired(Exception):
    """Raised by the tool runtime when a call needs human approval before it may run."""

    def __init__(self, decision: RiskDecision, tool: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"{tool} requires approval: {decision.reason}")
        self.decision, self.tool, self.arguments = decision, tool, arguments


def normalize_tool_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def arguments_hash(tool: str, arguments: Mapping[str, Any]) -> str:
    """Binds an approval to one exact action (tool + arguments, minus idempotency keys)."""
    args = {k: v for k, v in arguments.items() if k != "idempotency_key"}
    canon = json.dumps(
        {"tool": normalize_tool_name(tool), "args": args}, sort_keys=True, default=str
    )
    return hashlib.sha256(canon.encode()).hexdigest()


class ApprovalGrant(BaseModel):
    """A human's approval of exactly one action."""

    approval_id: str
    tool: str
    arguments_hash: str


class CallContext(BaseModel):
    tool: str
    risk: str
    server: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    origin: str = "trusted"  # "trusted" | "untrusted_only"
    step_id: str = ""
    diff_lines: int = 0
    files_touched: int = 0
    grant: ApprovalGrant | None = None


class Rule(BaseModel):
    name: str
    match: dict[str, Any]
    decision: str
    reason: str = ""
    when_not: dict[str, Any] | None = None


class Policy(BaseModel):
    rules: list[Rule]
    default: str = "require_approval"


def load_policy(path: Path | str | None = None) -> Policy:
    raw = yaml.safe_load(Path(path or DEFAULT_POLICY_PATH).read_text())
    policy = Policy.model_validate(raw)
    for r in policy.rules:
        if r.decision not in _STRICTNESS:
            raise ValueError(f"rule {r.name}: unknown decision {r.decision!r}")
    if policy.default not in _STRICTNESS:
        raise ValueError(f"unknown default decision {policy.default!r}")
    return policy


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


class RiskGate(Protocol):
    def decide(self, ctx: CallContext) -> RiskDecision: ...


class PolicyEngine:
    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or load_policy()

    # ------------------------------------------------------------------ matching
    def _matches(self, match: Mapping[str, Any], ctx: CallContext) -> bool:
        tool = normalize_tool_name(ctx.tool)
        for key, expected in match.items():
            if key == "tool":
                if tool not in {normalize_tool_name(t) for t in _as_list(expected)}:
                    return False
            elif key == "risk":
                if ctx.risk not in _as_list(expected):
                    return False
            elif key == "server":
                if ctx.server not in _as_list(expected):
                    return False
            elif key == "origin":
                if ctx.origin != expected:
                    return False
            elif key == "diff_lines_gt":
                if not ctx.diff_lines > int(expected):
                    return False
            elif key == "files_touched_gt":
                if not ctx.files_touched > int(expected):
                    return False
            elif key == "args":
                if not self._args_match(expected, ctx.arguments):
                    return False
            else:
                raise ValueError(f"unknown policy match key {key!r}")
        return True

    @staticmethod
    def _args_match(patterns: Mapping[str, str], arguments: Mapping[str, Any]) -> bool:
        for arg, pattern in patterns.items():
            if arg == "_any_branch":  # any branch-like argument must match
                values = [str(arguments[k]) for k in ("name", "branch", "head") if k in arguments]
                if not values or not all(re.search(pattern, v) for v in values):
                    return False
            elif not re.search(pattern, str(arguments.get(arg, ""))):
                return False
        return True

    def _rule_applies(self, rule: Rule, ctx: CallContext) -> bool:
        if not self._matches(rule.match, ctx):
            return False
        return not (rule.when_not and self._matches(rule.when_not, ctx))

    # ------------------------------------------------------------------ decision
    def decide(self, ctx: CallContext) -> RiskDecision:
        def result(decision: Any, reason: str, rule: str) -> RiskDecision:
            return RiskDecision(
                decision=decision,
                reason=reason,
                policy_rule=rule,
                tool=ctx.tool,
                step_id=ctx.step_id,
            )

        # 1. a deny rule always wins, wherever it appears. `when_not` on a deny rule means
        #    "deny unless this condition holds".
        for rule in self.policy.rules:
            if rule.decision == "deny" and self._matches(rule.match, ctx):
                if rule.when_not is None or not self._matches(rule.when_not, ctx):
                    return result("deny", rule.reason, rule.name)
        # 2. first matching non-deny rule
        decision, reason, name = self.policy.default, "default", "default"
        for rule in self.policy.rules:
            if rule.decision != "deny" and self._rule_applies(rule, ctx):
                decision, reason, name = rule.decision, rule.reason, rule.name
                break
        # 3. a human grant for EXACTLY this action turns require_approval into allow
        if decision == "require_approval" and ctx.grant is not None:
            same = ctx.grant.arguments_hash == arguments_hash(ctx.tool, ctx.arguments)
            if same and normalize_tool_name(ctx.grant.tool) == normalize_tool_name(ctx.tool):
                return result("allow", f"approved by human ({ctx.grant.approval_id})",
                              f"human_approved:{ctx.grant.approval_id}")  # fmt: skip
        return result(decision, reason, name)
