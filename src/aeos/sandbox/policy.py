"""Command policy and resource limits for the sandbox.

The container is the security boundary. This allowlist is defense in depth and guidance for
agents: commands are argv lists (no shell), and only known tools may be launched.
"""

from pydantic import BaseModel

ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "python", "python3", "pytest", "git", "ls", "cat", "head", "tail", "grep", "find", "wc",
        "node",
        "sort", "uniq", "diff", "patch", "sed", "mkdir", "cp", "mv", "rm", "touch", "echo", "true",
        "false", "pwd", "id", "env", "printf", "tr", "cut", "basename", "dirname", "sleep",
    }
)  # fmt: skip

# Commands that are always rejected, with a reason that is useful to an agent.
DENIED_COMMANDS: dict[str, str] = {
    "sh": "no shell; pass the command as an argv list",
    "bash": "no shell; pass the command as an argv list",
    "curl": "network access is disabled in the sandbox",
    "wget": "network access is disabled in the sandbox",
    "nc": "network access is disabled in the sandbox",
    "ssh": "network access is disabled in the sandbox",
    "scp": "network access is disabled in the sandbox",
    "pip": "installing packages is not allowed",
    "pip3": "installing packages is not allowed",
    "apt": "installing packages is not allowed",
    "apt-get": "installing packages is not allowed",
    "sudo": "privilege escalation is not allowed",
    "su": "privilege escalation is not allowed",
    "docker": "container tooling is not available",
    "mount": "mounting is not allowed",
    "chroot": "chroot is not allowed",
}

ALLOWED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {"init", "status", "diff", "log", "show", "apply", "add", "commit", "restore", "reset",
     "checkout", "rev-parse", "ls-files", "grep", "blame"}
)  # fmt: skip


class PolicyViolation(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def check_command(argv: list[str]) -> None:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise PolicyViolation("command must be a non-empty list of strings")
    if any("\x00" in a for a in argv):
        raise PolicyViolation("NUL byte in command")
    exe = argv[0]
    if "/" in exe:
        raise PolicyViolation(f"use a bare command name, not a path: {exe!r}")
    if exe in DENIED_COMMANDS:
        raise PolicyViolation(f"{exe!r} is not allowed: {DENIED_COMMANDS[exe]}")
    if exe not in ALLOWED_COMMANDS:
        raise PolicyViolation(f"{exe!r} is not on the sandbox command allowlist")
    if exe == "git" and (len(argv) < 2 or argv[1] not in ALLOWED_GIT_SUBCOMMANDS):
        sub = argv[1] if len(argv) > 1 else "<none>"
        raise PolicyViolation(
            f"git {sub!r} is not allowed (options before the subcommand are rejected)"
        )


class Limits(BaseModel):
    memory: str = "512m"
    cpus: float = 1.0
    pids: int = 128
    workspace_mb: int = 256
    tmp_mb: int = 64
    exec_timeout_s: float = 60.0
    max_output_bytes: int = 1_000_000
    max_file_bytes: int = 2_000_000
    ttl_s: float = 1800.0
