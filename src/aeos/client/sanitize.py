"""Everything an agent, tool or repo can influence is untrusted terminal input.

`clean` removes escape sequences and control characters; `safe` wraps the result in a rich `Text`
so `[markup]` inside it is never interpreted.
"""

import re

from rich.console import Console
from rich.text import Text

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC = re.compile(r"\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n


def clean(text: object) -> str:
    s = "" if text is None else str(text)
    s = _ESC.sub("", _CSI.sub("", _OSC.sub("", s)))
    return _CTRL.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def safe(text: object, style: str | None = None) -> Text:
    return Text(clean(text), style=style or "")


def say(console: Console, text: object, style: str | None = None) -> None:
    """Print untrusted-or-dynamic text inertly (no markup, no escapes)."""
    console.print(safe(text, style))
