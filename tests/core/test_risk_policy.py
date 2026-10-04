"""Policy matrix: allow / require_approval / deny, including bypass attempts."""

import itertools

import pytest

from aeos.core.risk import (
    ApprovalGrant,
    CallContext,
    PolicyEngine,
    arguments_hash,
    load_policy,
    normalize_tool_name,
)

ENGINE = PolicyEngine()


def decide(tool: str, risk: str, **kw: object) -> str:
    return ENGINE.decide(CallContext(tool=tool, risk=risk, **kw)).decision  # type: ignore[arg-type]


# ------------------------------------------------------------------ the basic matrix
@pytest.mark.parametrize(
    ("tool", "risk", "args", "expected"),
    [
        ("search_logs", "read", {}, "allow"),
        ("sandbox_exec", "read", {}, "allow"),
        ("create_branch", "write_reversible", {"name": "aeos/fix-1"}, "allow"),
        ("commit_patch", "write_reversible", {"branch": "aeos/fix-1"}, "allow"),
        ("create_pull_request", "write_irreversible", {"head": "aeos/fix-1"}, "require_approval"),
        ("post_to_slack", "write_irreversible", {}, "require_approval"),
        ("rotate_credentials", "write_irreversible", {}, "require_approval"),
        ("mystery_tool", "write_reversible", {}, "require_approval"),  # no rule -> default
        ("merge_pull_request", "write_irreversible", {}, "deny"),
        ("merge_pull_request", "read", {}, "deny"),  # even if it LIES about being read-only
        ("create_branch", "write_reversible", {"name": "main"}, "deny"),
        ("create_branch", "write_reversible", {"name": "feature/x"}, "deny"),
        ("commit_patch", "write_reversible", {"branch": "main"}, "deny"),
        ("create_pull_request", "write_irreversible", {"head": "feature/x"}, "deny"),
    ],
)
def test_matrix(tool: str, risk: str, args: dict[str, object], expected: str) -> None:
    assert decide(tool, risk, arguments=args) == expected


def test_large_diff_commit_needs_approval() -> None:
    args = {"branch": "aeos/x"}
    assert decide("commit_patch", "write_reversible", arguments=args, diff_lines=50) == "allow"
    assert (
        decide("commit_patch", "write_reversible", arguments=args, diff_lines=301)
        == "require_approval"
    )


def test_untrusted_only_origin_gates_other_writes() -> None:
    assert (
        decide("some_write", "write_reversible", origin="trusted") == "require_approval"
    )  # default
    # a *named* allow rule for reversible branch writes is not weakened by origin
    assert decide("create_branch", "write_reversible", origin="untrusted_only",
                  arguments={"name": "aeos/x"}) == "allow"  # fmt: skip
    # but read-only calls are never blocked by provenance
    assert decide("search_logs", "read", origin="untrusted_only") == "allow"


# ------------------------------------------------------------------ bypass attempts
@pytest.mark.parametrize(
    "alias",
    [
        "merge_pull_request",
        "Merge_Pull_Request",
        " merge-pull-request ",
        "MERGE__PULL__REQUEST",
        "merge_pr",
        "Merge-PR",
        "force_push",
        "delete_branch",
    ],
)
def test_aliasing_and_case_tricks_cannot_bypass_deny(alias: str) -> None:
    assert decide(alias, "write_irreversible") == "deny"
    assert decide(alias, "read") == "deny"


@pytest.mark.parametrize(
    "alias", ["create_pull_request", "Create-Pull-Request", " CREATE_PULL_REQUEST "]
)
def test_pr_name_variants_still_need_approval(alias: str) -> None:
    assert decide(alias, "write_irreversible", arguments={"head": "aeos/x"}) == "require_approval"


def test_renamed_irreversible_tool_is_still_gated_by_declared_risk() -> None:
    """An attacker/agent cannot rename `create_pull_request` to dodge the name rule."""
    assert decide("make_it_so", "write_irreversible") == "require_approval"
    assert (
        decide("publish_change", "write_irreversible", arguments={"head": "aeos/x"})
        == "require_approval"
    )


def test_unknown_risk_falls_through_to_the_default() -> None:
    assert decide("tool", "totally_new_risk") == "require_approval"


def test_branch_namespace_cannot_be_dodged_by_alternate_argument_names() -> None:
    # `name`, `branch` and `head` are all inspected; any non-aeos/ value denies
    assert decide("commit_patch", "write_reversible",
                  arguments={"branch": "aeos/ok", "head": "main"}) == "deny"  # fmt: skip
    assert decide("create_branch", "write_reversible", arguments={}) == "deny"  # no branch at all
    assert (
        decide("create_branch", "write_reversible", arguments={"name": "aeos/../main"}) == "allow"
    )
    # (the server independently rejects `..`; the policy layer only enforces the namespace prefix)


def test_normalisation() -> None:
    assert normalize_tool_name(" Merge-Pull Request ") == "merge_pull_request"


# ------------------------------------------------------------------ human grants
def grant(tool: str, args: dict[str, object]) -> ApprovalGrant:
    return ApprovalGrant(approval_id="ap-1", tool=tool, arguments_hash=arguments_hash(tool, args))


def test_exact_human_approval_allows_the_approved_action() -> None:
    args = {"title": "Fix N+1", "head": "aeos/x", "body": "b", "base": "main"}
    d = ENGINE.decide(CallContext(tool="create_pull_request", risk="write_irreversible",
                                  arguments=args, grant=grant("create_pull_request", args)))  # fmt: skip
    assert d.decision == "allow" and d.policy_rule == "human_approved:ap-1"


def test_approval_does_not_transfer_to_different_arguments() -> None:
    approved = {"title": "Fix N+1", "head": "aeos/x"}
    executed = {"title": "Something else entirely", "head": "aeos/x"}
    d = ENGINE.decide(CallContext(tool="create_pull_request", risk="write_irreversible",
                                  arguments=executed, grant=grant("create_pull_request", approved)))  # fmt: skip
    assert d.decision == "require_approval"


def test_approval_does_not_transfer_to_a_different_tool() -> None:
    args = {"head": "aeos/x"}
    d = ENGINE.decide(CallContext(tool="post_to_slack", risk="write_irreversible", arguments=args,
                                  grant=grant("create_pull_request", args)))  # fmt: skip
    assert d.decision == "require_approval"


def test_a_grant_can_never_override_a_deny() -> None:
    args: dict[str, object] = {}
    d = ENGINE.decide(CallContext(tool="merge_pull_request", risk="write_irreversible",
                                  arguments=args, grant=grant("merge_pull_request", args)))  # fmt: skip
    assert d.decision == "deny"
    args2 = {"name": "main"}
    d2 = ENGINE.decide(CallContext(tool="create_branch", risk="write_reversible", arguments=args2,
                                   grant=grant("create_branch", args2)))  # fmt: skip
    assert d2.decision == "deny"


def test_idempotency_key_does_not_change_the_approved_action_hash() -> None:
    a = arguments_hash("create_pull_request", {"title": "t", "idempotency_key": "k1"})
    b = arguments_hash("create_pull_request", {"title": "t", "idempotency_key": "k2"})
    assert a == b != arguments_hash("create_pull_request", {"title": "u"})


# ------------------------------------------------------------------ policy file hygiene
def test_every_decision_carries_a_reason_and_rule_name() -> None:
    for tool, risk in itertools.product(["create_pull_request", "search_logs", "weird"],
                                        ["read", "write_irreversible", "write_reversible"]):  # fmt: skip
        d = ENGINE.decide(CallContext(tool=tool, risk=risk, arguments={"head": "aeos/x"}))
        assert d.reason and d.policy_rule and d.tool == tool


def test_policy_file_rejects_unknown_decisions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "p.yaml"
    bad.write_text(
        "rules:\n  - name: x\n    match: {risk: read}\n    decision: maybe\ndefault: deny\n"
    )
    with pytest.raises(ValueError, match="unknown decision"):
        load_policy(bad)


def test_unknown_match_keys_fail_loudly_not_silently() -> None:
    from aeos.core.risk import Policy, Rule

    engine = PolicyEngine(
        Policy(rules=[Rule(name="typo", match={"riskk": "read"}, decision="allow")])
    )
    with pytest.raises(ValueError, match="unknown policy match key"):
        engine.decide(CallContext(tool="t", risk="read"))
