"""Human-in-the-loop decisions, shared by the API and the CLI.

A decision is applied exactly once (compare-and-set in the store) and produces the payload used to
resume the paused graph from its checkpoint.
"""

import asyncio
from typing import Any, Literal

from aeos.store import approvals


class DecisionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


async def apply_decision(
    approval_id: str,
    decision: Literal["approved", "rejected"],
    actor: str,
    feedback: str | None = None,
) -> tuple[approvals.ApprovalRequest, dict[str, Any]]:
    """Record the decision; returns (request, resume payload). Raises DecisionError."""
    result = await asyncio.to_thread(approvals.decide, approval_id, decision, actor, feedback)
    if result.kind == "not_found":
        raise DecisionError("not_found", f"no approval {approval_id}")
    if result.kind == "expired":
        raise DecisionError("expired", "this approval request has expired")
    if result.kind == "already_decided":
        assert result.request is not None
        raise DecisionError(
            "already_decided", f"already {result.request.status} by {result.request.actor}"
        )
    assert result.request is not None
    return result.request, {
        "decision": decision,
        "actor": actor,
        "feedback": feedback or "",
        "approval_id": approval_id,
    }


def expired_payload() -> dict[str, Any]:
    return dict(approvals.EXPIRED_PAYLOAD)


async def auto_approver(request: Any) -> dict[str, Any]:
    """Approves everything. For tests, demos with --yes, and scripted runs only."""
    return {"decision": "approved", "actor": "auto-approver", "feedback": ""}


def scripted_approver(*choices: dict[str, Any]) -> Any:
    """Approver that returns the given decisions in order (tests)."""
    queue = list(choices)

    async def approver(request: Any) -> dict[str, Any]:
        return queue.pop(0)

    return approver
