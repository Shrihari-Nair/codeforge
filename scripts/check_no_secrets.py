"""Fail if a secret could be committed: values from your local .env, or key-shaped strings.

  python scripts/check_no_secrets.py

Prints file:line and the NAME of what matched, never the value. Run by `make check` and CI.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".claude", ".venv", "node_modules", ".git", ".next", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", "__pycache__", "build", "htmlcov"}  # fmt: skip
SKIP_FILES = {".env", "uv.lock", "package-lock.json"}
SECRET_KEY_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|SALT|BASIC_AUTH")
PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
    "AWS access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"sk-[A-Za-z0-9]{32,}"),
    "Langfuse key": re.compile(r"[ps]k-lf-[A-Za-z0-9-]{8,}"),
}


def local_secret_values() -> dict[str, str]:
    path = ROOT / ".env"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            v = v.split(" #")[0].strip().strip("\"'")
            if SECRET_KEY_NAME.search(k) and len(v) >= 6:
                out[k.strip()] = v
    return out


def scan() -> list[str]:
    values, problems = local_secret_values(), []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(dirpath) / name
            if name in SKIP_FILES or (name.startswith(".env.") and name != ".env.example"):
                continue
            try:
                lines = path.read_text(errors="ignore").splitlines()
            except OSError:
                continue
            rel = path.relative_to(ROOT)
            for n, line in enumerate(lines, 1):
                problems += [
                    f"{rel}:{n}: value of {k} from .env" for k, v in values.items() if v in line
                ]
                problems += [
                    f"{rel}:{n}: looks like a {label}"
                    for label, rx in PATTERNS.items()
                    if rx.search(line)
                ]
    return problems


if __name__ == "__main__":
    found = scan()
    for p in found:
        print(p)
    print("secret scan:", f"{len(found)} problem(s)" if found else "clean")
    sys.exit(1 if found else 0)
