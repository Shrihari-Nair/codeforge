"""Escape/abuse tests. Everything hostile runs INSIDE the container, never on the host."""

import pytest

from aeos.sandbox import SandboxError, SandboxManager

pytestmark = pytest.mark.docker


def py(mgr: SandboxManager, sid: str, code: str, **kw: float) -> object:
    return mgr.exec(sid, ["python", "-c", code], **kw)  # type: ignore[arg-type]


def usable(mgr: SandboxManager, sid: str) -> bool:
    try:
        return mgr.exec(sid, ["true"], timeout=10).exit_code == 0
    except SandboxError:
        return False


def test_infinite_loop_is_killed_and_sandbox_survives(mgr: SandboxManager, sbx: str) -> None:
    r = mgr.exec(sbx, ["python", "-c", "while True: pass"], timeout=2)
    assert r.timed_out and r.exit_code != 0
    assert 1.8 <= r.duration_s < 10
    assert mgr.exec(sbx, ["python", "hello.py"]).stdout == "hi\n"  # workspace intact


def test_fork_bomb_is_contained_and_leaves_no_stragglers(mgr: SandboxManager) -> None:
    bomb = "import os\nwhile True:\n    os.fork()\n"
    with mgr.sandbox(files={"keep.txt": "data"}) as sid:
        r = mgr.exec(sid, ["python", "-c", bomb], timeout=3)
        assert mgr.client.containers.get(sid).attrs["HostConfig"]["PidsLimit"] == 128
        if r.sandbox_destroyed:
            assert sid not in mgr.list_sandboxes()
        else:  # cleaned up in place: no stragglers, workspace intact, still usable
            assert mgr._leftover_processes(sid) == 0
            assert mgr.exec(sid, ["cat", "keep.txt"]).stdout == "data"
        # and nothing the bomb started is still alive on the host
        assert sid not in mgr.list_sandboxes() or usable(mgr, sid)


def test_background_straggler_is_killed_when_the_command_returns(
    mgr: SandboxManager, sbx: str
) -> None:
    code = "import subprocess; subprocess.Popen(['sleep', '300'], start_new_session=True)"
    r = mgr.exec(sbx, ["python", "-c", code])
    assert r.exit_code == 0
    assert mgr._leftover_processes(sbx) == 0 or r.sandbox_destroyed


def test_memory_hog_is_killed_by_the_limit(mgr: SandboxManager, sbx: str) -> None:
    hog = "a = []\nwhile True:\n    a.append(b'x' * 10_000_000)\n"
    r = mgr.exec(sbx, ["python", "-c", hog], timeout=30)
    assert r.exit_code != 0  # OOM-killed (137) well before it can hurt the host
    assert usable(mgr, sbx) or sbx not in mgr.list_sandboxes()


def test_workspace_size_limit(mgr: SandboxManager, sbx: str) -> None:
    code = (
        "import errno\n"
        "try:\n"
        "    with open('big.bin', 'wb') as f:\n"
        "        for _ in range(400):\n"
        "            f.write(b'0' * 1_000_000)\n"
        "            f.flush()\n"
        "    print('wrote-all')\n"
        "except OSError as e:\n"
        "    print('ENOSPC' if e.errno == errno.ENOSPC else repr(e))\n"
    )
    r = mgr.exec(sbx, ["python", "-c", code], timeout=60)
    assert "ENOSPC" in r.stdout or r.exit_code != 0


def test_network_egress_is_impossible(mgr: SandboxManager, sbx: str) -> None:
    code = (
        "import socket, urllib.request\n"
        "for fn in (lambda: socket.gethostbyname('example.com'),\n"
        "           lambda: urllib.request.urlopen('http://1.1.1.1', timeout=3),\n"
        "           lambda: socket.create_connection(('8.8.8.8', 53), timeout=3)):\n"
        "    try:\n"
        "        fn()\n"
        "        print('REACHED')\n"
        "    except OSError:\n"
        "        print('blocked')\n"
    )
    r = mgr.exec(sbx, ["python", "-c", code], timeout=30)
    assert r.stdout.split() == ["blocked"] * 3
    nets = mgr.exec(sbx, ["cat", "/proc/net/dev"]).stdout
    interfaces = [ln.split(":")[0].strip() for ln in nets.splitlines()[2:]]
    assert interfaces == ["lo"]


def test_cannot_write_outside_workspace(mgr: SandboxManager, sbx: str) -> None:
    code = (
        "import os\n"
        "for p in ('/etc/pwned', '/usr/pwned', '/pwned', '/bin/pwned', '/proc/pwned'):\n"
        "    try:\n"
        "        open(p, 'w').write('x')\n"
        "        print('WROTE', p)\n"
        "    except OSError:\n"
        "        print('denied')\n"
        "open('/workspace/ok.txt', 'w').write('x'); print('workspace-ok')\n"
    )
    r = mgr.exec(sbx, ["python", "-c", code])
    assert r.stdout.split() == ["denied"] * 5 + ["workspace-ok"]


def test_host_paths_are_not_visible(mgr: SandboxManager, sbx: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOP SECRET")
    code = (
        "import os\n"
        f"print(os.path.exists({str(secret)!r}))\n"
        "print(os.path.exists('/home/srihari'))\n"
        "print(os.path.exists('/var/run/docker.sock'))\n"
        "print(os.path.exists('/run/docker.sock'))\n"
    )
    assert mgr.exec(sbx, ["python", "-c", code]).stdout.split() == ["False"] * 4


def test_runs_unprivileged_with_no_capabilities(mgr: SandboxManager, sbx: str) -> None:
    code = (
        "import os\n"
        "print(os.getuid())\n"
        "try:\n"
        "    os.setuid(0); print('ROOT')\n"
        "except PermissionError:\n"
        "    print('no-root')\n"
        "print([ln for ln in open('/proc/self/status') if ln.startswith('CapEff')][0].split()[1])\n"
    )
    out = mgr.exec(sbx, ["python", "-c", code]).stdout.split()
    assert out == ["10001", "no-root", "0000000000000000"]


def test_output_flood_is_capped(mgr: SandboxManager, sbx: str) -> None:
    r = mgr.exec(sbx, ["python", "-c", "print('x' * 10_000_000)"])
    assert r.truncated and len(r.stdout) <= 1_000_000
    assert usable(mgr, sbx)


def test_symlink_in_workspace_cannot_reach_host(mgr: SandboxManager, sbx: str) -> None:
    mgr.exec(sbx, ["python", "-c", "import os; os.symlink('/', '/workspace/root')"])
    r = mgr.exec(sbx, ["ls", "root/home"])
    assert "srihari" not in r.stdout  # symlinks resolve inside the container, not the host
