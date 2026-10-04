import pytest

from aeos.core.retry import RetryPolicy

P = RetryPolicy()


@pytest.mark.parametrize(("attempts", "expected"), [(1, "retry"), (2, "escalate"), (5, "escalate")])
def test_step_failures_retry_then_escalate(attempts: int, expected: str) -> None:
    assert P.on_step_failure(attempts) == expected


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(1, "replan"), (2, "replan_new_strategy"), (3, "escalate"), (9, "escalate")],
)
def test_verification_failures_replan_change_strategy_then_escalate(
    failures: int, expected: str
) -> None:
    assert P.on_verification_failure(failures) == expected


def test_policy_is_configurable() -> None:
    strict = RetryPolicy(max_verification_failures=2, strategy_change_after=1, max_step_attempts=1)
    assert strict.on_verification_failure(1) == "replan_new_strategy"
    assert strict.on_verification_failure(2) == "escalate"
    assert strict.on_step_failure(1) == "escalate"
