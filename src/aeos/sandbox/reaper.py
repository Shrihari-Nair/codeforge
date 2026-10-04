"""Orphan reaper: removes sandboxes that outlived their task or crashed owner.

uv run python -m aeos.sandbox.reaper --ttl 1800 [--interval 60]
"""

import argparse
import logging
import time

from aeos.sandbox.manager import SandboxManager

log = logging.getLogger("aeos.sandbox.reaper")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ttl", type=float, default=1800.0)
    parser.add_argument("--interval", type=float, default=0.0, help="0 = run once")
    args = parser.parse_args()
    mgr = SandboxManager()
    while True:
        removed = mgr.reap(ttl_s=args.ttl)
        log.info("reaped %d sandbox(es)", len(removed), extra={"removed": removed})
        print(f"reaped: {removed}")
        if not args.interval:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
