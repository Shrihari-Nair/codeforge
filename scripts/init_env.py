"""Create a .env from .env.example with freshly generated local secrets.

  python scripts/init_env.py            # writes ./.env (refuses to overwrite)
  python scripts/init_env.py --out X    # write somewhere else

You still add AEOS_GEMINI_API_KEY (and, for real PRs, AEOS_GITHUB_REPO / AEOS_GITHUB_TOKEN)
yourself. Nothing here is a real credential: the Langfuse/Redis/MinIO values only protect the
local containers.
"""

import argparse
import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate() -> dict[str, str]:
    pub, sec = f"pk-lf-{secrets.token_hex(12)}", f"sk-lf-{secrets.token_hex(12)}"
    return {
        "AEOS_LANGFUSE_PUBLIC_KEY": pub,
        "AEOS_LANGFUSE_SECRET_KEY": sec,
        "LANGFUSE_BASIC_AUTH": base64.b64encode(f"{pub}:{sec}".encode()).decode(),
        "LANGFUSE_SALT": secrets.token_hex(16),
        "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_NEXTAUTH_SECRET": secrets.token_hex(16),
        "LANGFUSE_INIT_PASSWORD": secrets.token_urlsafe(12),
        "LANGFUSE_MINIO_PASSWORD": secrets.token_hex(12),
        "LANGFUSE_REDIS_PASSWORD": secrets.token_hex(12),
        "AEOS_A2A_API_KEY": secrets.token_hex(16),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / ".env"))
    out = Path(ap.parse_args().out)
    if out.exists():
        print(f"{out} already exists; not overwriting it.", file=sys.stderr)
        return 1
    values = generate()
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key, sep, rest = line.partition("=")
        if sep and key in values:
            comment = rest.partition("#")[2]
            line = f"{key}={values[key]}" + (f"   # {comment.strip()}" if comment else "")
        lines.append(line)
    out.write_text("\n".join(lines) + "\n")
    out.chmod(0o600)
    print(f"wrote {out}. Now add AEOS_GEMINI_API_KEY (and AEOS_GITHUB_* for real PRs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
