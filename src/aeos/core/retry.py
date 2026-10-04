"""Retry policy as an explicit state machine (no LLM involved).

Step failures (a specialist erroring/looping): retry the same step up to `max_step_attempts`,
then escalate to a human. Verification failures: 1st -> replan, 2nd -> replan that MUST change
strategy (re-investigate), 3rd -> escalate to a human.
"""

from dataclasses import dataclass
from typing import Literal

StepAction = Literal["retry", "escalate"]
VerificationAction = Literal["replan", "replan_new_strategy", "escalate"]


@dataclass(frozen=True)
class RetryPolicy:
    max_step_attempts: int = 2
    max_verification_failures: int = 3
    strategy_change_after: int = 2  # from this many failed verifications on, re-investigate

    def on_step_failure(self, attempts_made: int) -> StepAction:
        return "retry" if attempts_made < self.max_step_attempts else "escalate"

    def on_verification_failure(self, failures_so_far: int) -> VerificationAction:
        if failures_so_far >= self.max_verification_failures:
            return "escalate"
        if failures_so_far >= self.strategy_change_after:
            return "replan_new_strategy"
        return "replan"
