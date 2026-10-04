from collections.abc import Iterator

import docker
import pytest

from aeos.sandbox import SandboxManager


@pytest.fixture(scope="session")
def mgr() -> Iterator[SandboxManager]:
    try:
        docker.from_env().ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker not available")
    manager = SandboxManager()
    yield manager
    manager.reap(keep=set())  # leave nothing behind


@pytest.fixture
def sbx(mgr: SandboxManager) -> Iterator[str]:
    with mgr.sandbox(task_id="test", files={"hello.py": "print('hi')\n"}) as sid:
        yield sid
