"""Exit non-zero if any compose service is not running/healthy."""

import json
import subprocess
import sys


def unhealthy(ps_json_lines: str) -> list[str]:
    rows = [json.loads(x) for x in ps_json_lines.splitlines() if x.strip()]
    if not rows:
        return ["<no services running>"]
    bad = []
    for r in rows:
        healthy = r.get("Health", "") in ("", "healthy")
        if r.get("State") != "running" or not healthy:
            bad.append(r["Service"])
    return bad


def main() -> int:
    out = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        print(f"docker compose failed: {out.stderr.strip()}", file=sys.stderr)
        return 2
    bad = unhealthy(out.stdout)
    if bad:
        print(f"UNHEALTHY: {', '.join(bad)}", file=sys.stderr)
        return 1
    print("all services healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
