import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aeos.mcp_client import McpClient, McpToolError

GT = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())
BAD = GT["root_cause"]["commit_sha"]
GOOD = GT["last_good_commit_sha"]


async def fix_patch(c: McpClient) -> str:
    """The acceptable fix, as a unified diff: revert catalog.py to the batched version."""
    res = await c.call("get_diff", {"base": BAD, "head": GOOD, "path": "checkout/catalog.py"})
    assert res.data is not None
    return str(res.data["diff"])


async def test_tools_and_risk_levels_and_no_merge_tool(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        tools = await c.list_tools()
    risks = {n: t.risk for n, t in tools.items()}
    assert risks["create_pull_request"] == "write_irreversible"
    assert risks["create_branch"] == risks["commit_patch"] == "write_reversible"
    for read in (
        "get_blame",
        "get_repository",
        "search_code",
        "get_file",
        "list_commits",
        "get_diff",
        "get_pull_request",
    ):
        assert risks[read] == "read"
    assert not any("merge" in n for n in tools)
    for n in ("create_branch", "commit_patch", "create_pull_request"):
        assert "idempotency_key" in tools[n].input_schema["required"]


async def test_get_repository(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        repo = (await c.call("get_repository")).data
    assert repo is not None
    assert repo["name"] == "checkout-service" and repo["default_branch"] == "main"
    assert repo["commit_count"] == 20 and len(repo["head_sha"]) == 40
    assert [b["name"] for b in repo["branches"]] == ["main"]


async def test_list_commits_and_path_filter(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        allc = (await c.call("list_commits", {"limit": 50})).data
        cat = (await c.call("list_commits", {"path": "checkout/catalog.py"})).data
    assert allc is not None and cat is not None
    assert len(allc["result"]) == 20
    assert [x["sha"] for x in cat["result"]][0] == BAD  # newest catalog change is the regression
    assert len(cat["result"]) == 2


async def test_get_file_at_two_refs_differs(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        good = (await c.call("get_file", {"path": "checkout/catalog.py", "ref": GOOD})).data
        bad = (await c.call("get_file", {"path": "checkout/catalog.py", "ref": "main"})).data
    assert good is not None and bad is not None
    assert "IN (" in good["content"] and "IN (" not in bad["content"]
    assert "WHERE id = ?" in bad["content"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"path": "../etc/passwd"}, "invalid_argument"),
        ({"path": "/etc/passwd"}, "invalid_argument"),
        ({"path": ".git/config"}, "invalid_argument"),
        ({"path": ":(top)README.md"}, "invalid_argument"),
        ({"path": "checkout"}, "invalid_argument"),  # directory
        ({"path": "nope.py"}, "not_found"),
        ({"path": "README.md", "ref": "--output=/tmp/pwned"}, "invalid_argument"),
        ({"path": "README.md", "ref": "main..HEAD"}, "invalid_argument"),
        ({"path": "README.md", "ref": "no-such-branch"}, "not_found"),
    ],
)
async def test_get_file_abuse_is_rejected(gh, args: dict[str, Any], code: str) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        with pytest.raises(McpToolError) as ei:
            await c.call("get_file", args)
    assert ei.value.code == code


async def test_search_code(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        hits = (await c.call("search_code", {"pattern": "get_product", "ref": "main"})).data
        regex = (await c.call("search_code", {"pattern": "def get_prod\\w+", "regex": True})).data
        opt = (await c.call("search_code", {"pattern": "--help"})).data  # a pattern, not a flag
        scoped = (await c.call("search_code", {"pattern": "def ", "path": "checkout/db.py"})).data
    assert hits is not None and regex is not None and opt is not None and scoped is not None
    assert {"checkout/catalog.py", "checkout/service.py"} <= {m["path"] for m in hits["matches"]}
    assert len(regex["matches"]) == 2
    assert opt["matches"] == []
    assert {m["path"] for m in scoped["matches"]} == {"checkout/db.py"}


async def test_search_code_rejects_bad_ref(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        with pytest.raises(McpToolError) as ei:
            await c.call("search_code", {"pattern": "x", "ref": "-v"})
    assert ei.value.code == "invalid_argument"


async def test_get_diff_stats_and_truncation(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        d = (await c.call("get_diff", {"base": f"{BAD}~1", "head": BAD})).data
        cut = (await c.call("get_diff", {"base": f"{BAD}~1", "head": BAD, "max_bytes": 100})).data
        with pytest.raises(McpToolError) as ei:
            await c.call("get_diff", {"base": "main", "head": "main", "max_bytes": 5})
    assert d is not None and cut is not None
    assert [f["path"] for f in d["files"]] == ["checkout/catalog.py"]
    assert d["truncated"] is False and cut["truncated"] is True and len(cut["diff"]) <= 100
    assert ei.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "refs/heads/main",
        "aeos/../x",
        "feature/x",
        "aeos/a b",
        "aeos/x.lock",
        "aeos/",
        "aeos//x",
        "-aeos/x",
        "aeos/x;rm -rf",
    ],
)
async def test_create_branch_rejects_bad_names(gh, name: str) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        with pytest.raises(McpToolError) as ei:
            await c.call("create_branch", {"name": name, "idempotency_key": "k"})
    assert ei.value.code == "invalid_argument"


async def test_create_branch_is_idempotent_and_conflicts_on_different_base(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        a = (await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "k1"})).data
        b = (await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "k1"})).data
        with pytest.raises(McpToolError) as ei:
            await c.call(
                "create_branch", {"name": "aeos/fix-1", "idempotency_key": "k2", "from_ref": GOOD}
            )
        repo = (await c.call("get_repository")).data
    assert a is not None and b is not None and repo is not None
    assert a["created"] is True and b["created"] is False and a["sha"] == b["sha"]
    assert ei.value.code == "conflict"
    assert sorted(x["name"] for x in repo["branches"]) == ["aeos/fix-1", "main"]


async def test_commit_patch_applies_fix_and_replay_is_exactly_once(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "b1"})
        patch = await fix_patch(c)
        args = {
            "branch": "aeos/fix-1",
            "patch": patch,
            "idempotency_key": "c1",
            "message": "fix(catalog): restore batched, cached product lookup",
        }
        first = (await c.call("commit_patch", args)).data
        replay = (await c.call("commit_patch", args)).data
        commits = (await c.call("list_commits", {"ref": "aeos/fix-1", "limit": 50})).data
        content = (
            await c.call("get_file", {"path": "checkout/catalog.py", "ref": "aeos/fix-1"})
        ).data
        main_commits = (await c.call("list_commits", {"limit": 50})).data
    assert first is not None and replay is not None and commits is not None
    assert first["replayed"] is False and replay["replayed"] is True
    assert first["commit_sha"] == replay["commit_sha"]
    assert len(commits["result"]) == 21  # exactly one new commit despite two calls
    assert content is not None and "IN (" in content["content"]
    assert main_commits is not None and len(main_commits["result"]) == 20  # main untouched


async def test_commit_patch_guards(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        await c.call("create_branch", {"name": "aeos/x", "idempotency_key": "b"})
        base = {"idempotency_key": "k", "message": "m"}
        cases = [
            ({"branch": "main", "patch": "x"}, "invalid_argument"),  # never commit to main
            ({"branch": "aeos/missing", "patch": "x"}, "not_found"),
            ({"branch": "aeos/x", "patch": ""}, "invalid_argument"),
            ({"branch": "aeos/x", "patch": "not a patch"}, "conflict"),
            (
                {
                    "branch": "aeos/x",
                    "patch": "diff --git a/../evil b/../evil\n--- /dev/null\n"
                    "+++ b/../evil\n@@ -0,0 +1 @@\n+x\n",
                },
                "conflict",
            ),
        ]
        for extra, code in cases:
            with pytest.raises(McpToolError) as ei:
                await c.call("commit_patch", {**base, **extra})
            assert ei.value.code == code, extra
        with pytest.raises(McpToolError) as ei:
            await c.call(
                "commit_patch",
                {"branch": "aeos/x", "patch": "x", "message": " ", "idempotency_key": "k9"},
            )
        assert ei.value.code == "invalid_argument"
        repo = (await c.call("get_repository")).data
    assert repo is not None
    shas = {b["name"]: b["sha"] for b in repo["branches"]}
    assert shas["aeos/x"] == repo["head_sha"]  # nothing was committed by the failed attempts


async def test_pull_request_flow_is_idempotent_and_never_touches_main(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        before = (await c.call("get_repository")).data
        await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "b1"})
        with pytest.raises(McpToolError) as ei:  # no changes yet
            await c.call(
                "create_pull_request",
                {"title": "t", "body": "b", "head": "aeos/fix-1", "idempotency_key": "p0"},
            )
        assert ei.value.code == "invalid_argument"
        await c.call(
            "commit_patch",
            {
                "branch": "aeos/fix-1",
                "patch": await fix_patch(c),
                "message": "fix",
                "idempotency_key": "c1",
            },
        )
        args = {
            "title": "Fix N+1 in catalog",
            "body": "restores batching",
            "head": "aeos/fix-1",
            "idempotency_key": "p1",
        }
        pr1 = (await c.call("create_pull_request", args)).data
        pr2 = (await c.call("create_pull_request", args)).data  # replay
        with pytest.raises(McpToolError) as ei:  # same head/base, different key
            await c.call("create_pull_request", {**args, "idempotency_key": "p2"})
        got = (await c.call("get_pull_request", {"number": pr1["number"]})).data  # type: ignore[index]
        after = (await c.call("get_repository")).data
        with pytest.raises(McpToolError) as nf:
            await c.call("get_pull_request", {"number": 999})
    assert pr1 is not None and pr2 is not None and got is not None
    assert pr1["number"] == pr2["number"] == 1
    assert pr1["replayed"] is False and pr2["replayed"] is True
    assert ei.value.code == "conflict" and nf.value.code == "not_found"
    assert got["status"] == "open" and [f["path"] for f in got["files"]] == ["checkout/catalog.py"]
    assert before is not None and after is not None and after["head_sha"] == before["head_sha"]


async def test_pull_request_requires_aeos_branch(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        for head in ("main", "feature/x"):
            with pytest.raises(McpToolError) as ei:
                await c.call(
                    "create_pull_request",
                    {"title": "t", "body": "b", "head": head, "idempotency_key": "p"},
                )
            assert ei.value.code == "invalid_argument"


async def test_pushed_branch_really_exists_in_the_git_remote(gh) -> None:  # type: ignore[no-untyped-def]
    async with gh() as c:
        await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "b1"})
        await c.call(
            "commit_patch",
            {
                "branch": "aeos/fix-1",
                "patch": await fix_patch(c),
                "message": "fix",
                "idempotency_key": "c1",
            },
        )
        remote = gh.remote  # type: ignore[attr-defined]
    proc = await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(remote), "log", "--format=%an|%s", "-1", "aeos/fix-1"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout.strip()
    assert out == "AEOS Agent|fix"


async def test_create_branch_replay_after_the_branch_advanced_returns_the_original_result(
    gh,
) -> None:  # type: ignore[no-untyped-def]
    """Regression found by the chaos tests: a worker dies after commit_patch; on resume it replays
    create_branch with the same idempotency key. The branch has moved on (our own commit), but the
    replay must succeed with the original result instead of reporting a conflict."""
    async with gh() as c:
        first = (
            await c.call("create_branch", {"name": "aeos/fix-9", "idempotency_key": "kb"})
        ).data
        await c.call("commit_patch", {"branch": "aeos/fix-9", "patch": await fix_patch(c),
                                      "message": "fix", "idempotency_key": "kc"})  # fmt: skip
        replay = (
            await c.call("create_branch", {"name": "aeos/fix-9", "idempotency_key": "kb"})
        ).data
        with pytest.raises(McpToolError) as ei:  # a DIFFERENT key is still a real conflict
            await c.call("create_branch", {"name": "aeos/fix-9", "idempotency_key": "other"})
    assert first is not None and replay is not None
    assert replay["sha"] == first["sha"] and replay["created"] is False
    assert ei.value.code == "conflict"


async def test_get_blame_points_at_the_commit_that_changed_the_current_value(gh) -> None:  # type: ignore[no-untyped-def]
    """Regression found by the live eval: models blamed the commit that CREATED a file, not the one
    that changed the line. get_blame names the right one."""
    async with gh() as c:
        whole = (await c.call("get_blame", {"path": "checkout/catalog.py"})).data
        assert whole is not None
        bad_line = next(x for x in whole["lines"] if "WHERE id = ?" in x["text"])
        focused = (await c.call("get_blame", {"path": "checkout/catalog.py", "ref": "main",
                                              "start_line": bad_line["line"],
                                              "end_line": bad_line["line"]})).data  # fmt: skip
        with pytest.raises(McpToolError) as ei:
            await c.call(
                "get_blame", {"path": "checkout/catalog.py", "start_line": 5, "end_line": 2}
            )
        with pytest.raises(McpToolError) as nf:
            await c.call("get_blame", {"path": "nope.py"})
        with pytest.raises(McpToolError) as tr:
            await c.call("get_blame", {"path": "../etc/passwd"})
    assert bad_line["sha"] == BAD  # the regression commit, not the commit that created the file
    assert "simplify product lookup" in bad_line["summary"]
    assert focused is not None and [x["sha"] for x in focused["lines"]] == [BAD]
    creators = {x["sha"] for x in whole["lines"]}
    assert len(creators) >= 2  # some lines still belong to older commits: blame is per line
    assert ei.value.code == "invalid_argument"
    assert (
        nf.value.code in ("not_found", "invalid_argument") and tr.value.code == "invalid_argument"
    )
