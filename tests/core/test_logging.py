import asyncio
import io
import json
import logging
from pathlib import Path

import pytest

from aeos.core.logging import configure_logging, run_context


@pytest.fixture
def stream() -> io.StringIO:
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    return buf


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(x) for x in buf.getvalue().splitlines() if x]


def test_emits_json_with_context(stream: io.StringIO) -> None:
    log = logging.getLogger("t")
    with run_context(task_id="t1", run_id="r1", agent="supervisor"):
        log.info("hello", extra={"step": 3})
    rec = lines(stream)[0]
    assert rec["message"] == "hello"
    assert rec["task_id"] == "t1" and rec["run_id"] == "r1" and rec["agent"] == "supervisor"
    assert rec["step"] == 3
    assert rec["level"] == "INFO"


def test_no_context_defaults_to_null(stream: io.StringIO) -> None:
    logging.getLogger("t").info("x")
    rec = lines(stream)[0]
    assert rec["task_id"] is None and rec["agent"] is None


def test_nested_context_overrides_only_given_fields(stream: io.StringIO) -> None:
    with run_context(task_id="t1", agent="a"):
        with run_context(agent="b"):
            logging.getLogger("t").info("x")
    rec = lines(stream)[0]
    assert rec["task_id"] == "t1" and rec["agent"] == "b"


async def test_context_isolated_between_concurrent_tasks(stream: io.StringIO) -> None:
    async def work(name: str) -> None:
        with run_context(task_id=name):
            await asyncio.sleep(0.01)
            logging.getLogger("t").info(name)

    await asyncio.gather(work("A"), work("B"))
    recs = [r for r in lines(stream) if r["logger"] == "t"]
    assert len(recs) == 2
    for rec in recs:
        assert rec["task_id"] == rec["message"]


def test_non_serializable_extra_does_not_crash(stream: io.StringIO) -> None:
    logging.getLogger("t").info("x", extra={"p": Path("/tmp"), "e": ValueError("boom")})
    rec = lines(stream)[0]
    assert "tmp" in str(rec["p"]) and "boom" in str(rec["e"])


def test_exception_info_included(stream: io.StringIO) -> None:
    try:
        raise RuntimeError("bad")
    except RuntimeError:
        logging.getLogger("t").exception("failed")
    assert "RuntimeError" in str(lines(stream)[0]["exc_info"])


def test_configure_is_idempotent(stream: io.StringIO) -> None:
    configure_logging("DEBUG", stream=stream)
    logging.getLogger("t").info("once")
    assert len(lines(stream)) == 1
