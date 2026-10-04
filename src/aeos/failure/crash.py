"""Crash injection: SIGKILL this process at a named point (kill -9 semantics, no cleanup).

Used by chaos tests and the failure-injection phase. Enabled only by the environment variable
AEOS_CRASH_AT=point[,point...]; a no-op otherwise. Points look like:
  node:<name>:before | node:<name>:after      (around a graph node)
  tool:<tool_name>:before | tool:<tool_name>:after   (around a tool call)
"""

import os
import signal


def maybe_crash(point: str) -> None:
    spec = os.environ.get("AEOS_CRASH_AT")
    if spec and point in {p.strip() for p in spec.split(",")}:
        os.kill(os.getpid(), signal.SIGKILL)
