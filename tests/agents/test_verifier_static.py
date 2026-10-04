from aeos.agents.verifier import GateConfig, parse_patch, static_gates

CATALOG_FIX = """diff --git a/checkout/catalog.py b/checkout/catalog.py
index 111..222 100644
--- a/checkout/catalog.py
+++ b/checkout/catalog.py
@@ -1,3 +1,3 @@
-old line
+new line
"""


def gates(patch: str, **cfg: object) -> dict[str, bool]:
    return static_gates(patch, GateConfig(**cfg))[0]  # type: ignore[arg-type]


def test_parse_patch_statuses_and_counts() -> None:
    patch = CATALOG_FIX + (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    files = {f.path: f for f in parse_patch(patch)}
    assert (
        files["checkout/catalog.py"].status == "modified"
        and files["checkout/catalog.py"].added == 1
    )
    assert files["new.py"].status == "new" and files["new.py"].added == 2
    assert files["gone.py"].status == "deleted" and files["gone.py"].removed == 1


def test_clean_patch_passes_static_gates() -> None:
    assert all(gates(CATALOG_FIX).values())


def test_measurement_files_are_protected() -> None:
    for path in ("benchmark.py", "checkout/costs.py"):
        patch = CATALOG_FIX.replace("checkout/catalog.py", path)
        g, issues = static_gates(patch, GateConfig())
        assert not g["paths_allowed"] and issues[0].type == "forbidden_edit"


def test_existing_tests_cannot_be_modified_but_new_tests_can() -> None:
    modified = CATALOG_FIX.replace("checkout/catalog.py", "tests/test_service.py")
    assert not gates(modified)["paths_allowed"]
    new = ("diff --git a/tests/test_new.py b/tests/test_new.py\nnew file mode 100644\n"
           "--- /dev/null\n+++ b/tests/test_new.py\n@@ -0,0 +1 @@\n+def test_x(): pass\n")  # fmt: skip
    assert gates(new)["paths_allowed"]


def test_diff_size_limits() -> None:
    assert not gates(CATALOG_FIX, max_diff_lines=1)["diff_size_ok"]
    many = "".join(CATALOG_FIX.replace("catalog", f"f{i}") for i in range(6))
    assert not gates(many)["diff_size_ok"]


def test_secret_detection_patterns_and_exact_values() -> None:
    def with_line(line: str) -> str:
        return CATALOG_FIX + f"+{line}\n"

    for secret in ('API_KEY = "abcd1234efgh5678"', "key=" + "AKIA" + "ABCDEFGHIJKLMNOP",
                   "-----BEGIN RSA PRIVATE " + "KEY-----", "t = 'ghp_" + "a" * 36 + "'"):  # fmt: skip
        assert not gates(with_line(secret))["no_secrets_in_diff"], secret
    assert gates(with_line("x = compute_total(items)"))["no_secrets_in_diff"]
    assert not gates(with_line("note: s3cr3t-value-123"), secret_values=["s3cr3t-value-123"])[
        "no_secrets_in_diff"
    ]
