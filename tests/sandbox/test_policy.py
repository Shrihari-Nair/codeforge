import pytest

from aeos.sandbox.policy import PolicyViolation, check_command


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "pytest", "-q"],
        ["python", "benchmark.py", "--json"],
        ["git", "diff", "--cached"],
        ["ls", "-la"],
        ["grep", "-rn", "get_product", "."],
    ],
)
def test_allowed(argv: list[str]) -> None:
    check_command(argv)


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (["curl", "http://x"], "network"),
        (["wget", "http://x"], "network"),
        (["sh", "-c", "id"], "shell"),
        (["bash"], "shell"),
        (["pip", "install", "x"], "installing"),
        (["sudo", "ls"], "privilege"),
        (["/usr/bin/python"], "bare command"),
        (["./evil"], "bare command"),
        (["nmap"], "allowlist"),
        (["git", "-c", "core.sshCommand=evil", "fetch"], "not allowed"),
        (["git", "push"], "not allowed"),
        (["git", "clone", "http://x"], "not allowed"),
        (["git"], "not allowed"),
        ([], "non-empty"),
        (["python", "a\x00b"], "NUL"),
    ],
)
def test_denied(argv: list[str], fragment: str) -> None:
    with pytest.raises(PolicyViolation) as ei:
        check_command(argv)
    assert fragment in ei.value.message
