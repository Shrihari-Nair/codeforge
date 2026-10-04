import importlib.util
from pathlib import Path


def test_repository_contains_no_secrets() -> None:
    spec = importlib.util.spec_from_file_location(
        "check_no_secrets", Path(__file__).parents[1] / "scripts" / "check_no_secrets.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.scan() == []
