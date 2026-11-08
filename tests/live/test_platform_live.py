import os
import re
import signal
import socket
import subprocess
import sys
import time

import pytest

from gsched.platform import JobIdentity

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def dp():
    from gsched.platform.darwin import DarwinPlatform
    return DarwinPlatform()


def test_live_layout_check(dp):
    assert dp.layout_report()["ok"]
    m = dp.machine()
    assert m.memsize_bytes > 2**30 and m.page_size in (4096, 16384) and m.physical_cpu >= 1


def _vm_stat_reclaimable():
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+) bytes", out).group(1))
    def pages(label):
        return int(re.search(label + r":\s+(\d+)\.", out).group(1))
    return (pages("Pages free") + pages("Pages purgeable") + pages("File-backed pages")) * page


def test_live_mem_sample_matches_vm_stat(dp):
    best = None
    for _ in range(5):
        a = dp.mem_sample(0).reclaimable_bytes
        b = _vm_stat_reclaimable()
        c = dp.mem_sample(0).reclaimable_bytes
        err = abs(b - (a + c) / 2) / max(b, 1)
        best = err if best is None else min(best, err)
        if best < 0.05:
            break
    assert best < 0.05, best
    s = dp.mem_sample(0)
    assert s.swap_used_bytes is not None and s.level is not None


def test_live_footprint_of_self_positive(dp):
    p = dp.proc(os.getpid())
    assert p is not None and p.footprint_bytes and p.footprint_bytes > 0
    assert p.uid == os.getuid() and p.path


def _sleeper():
    return subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)


def test_live_identity_of_spawned_sleep(dp):
    s = _sleeper()
    try:
        p = dp.proc(s.pid)
        ident = JobIdentity(p.pid, p.start_sec, p.start_usec, p.path, p.pgid)
        assert dp.identity_matches(ident) and p.pgid == s.pid and p.path.endswith("sleep")
        assert any(x.pid == s.pid for x in dp.processes())
        assert dp.proc_argv(s.pid) == ["/bin/sleep", "30"]
        assert dp.pid_alive(s.pid)
        bad = JobIdentity(p.pid, p.start_sec, p.start_usec + 1, p.path, p.pgid)
        assert not dp.signal_pid(bad, signal.SIGTERM)
        assert s.poll() is None
        assert dp.signal_group(ident, signal.SIGTERM)
        s.wait(timeout=5)
    finally:
        if s.poll() is None:
            s.kill()
            s.wait()


def test_live_identity_mismatch_after_exit(dp):
    s = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    p = dp.proc(s.pid)
    ident = JobIdentity(p.pid, p.start_sec, p.start_usec, p.path, p.pgid)
    s.kill()
    s.wait()
    assert not dp.identity_matches(ident)
    assert not dp.signal_pid(ident, signal.SIGTERM)
    assert not dp.pid_alive(s.pid)


def test_live_zombie_is_not_alive(dp):
    s = subprocess.Popen(["/usr/bin/true"])
    for _ in range(100):
        time.sleep(0.02)
        if not dp.pid_alive(s.pid):
            break
    assert not dp.pid_alive(s.pid)
    s.wait()


def test_live_peer_credentials_socketpair(dp):
    a, b = socket.socketpair()
    try:
        assert dp.peer_credentials(a) == (os.getuid(), os.getpid())
    finally:
        a.close()
        b.close()


def test_live_port_probe(dp):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert dp.port_listening(port)
    finally:
        srv.close()
    assert not dp.port_listening(port)


def test_live_gpu_stats_parse(dp):
    g = dp.gpu_stats()
    for v in (g.device_util_pct, g.in_use_bytes, g.alloc_bytes):
        assert v is None or isinstance(v, int)


def test_live_reap_and_spawn_shim_args(dp, tmp_path):
    out, err = tmp_path / "o", tmp_path / "e"
    pid = dp.spawn_shim([sys.executable, "-c", "print('hi')"], env={"PATH": "/usr/bin:/bin"}, cwd=str(tmp_path),
                        stdout_path=str(out), stderr_path=str(err), pass_fds=())
    got = []
    for _ in range(200):
        got += [g for g in dp.reap() if g[0] == pid]
        if got:
            break
        time.sleep(0.02)
    assert got and got[0][0] == pid and out.read_text() == "hi\n"


def test_live_kern_procargs_self(dp):
    argv = dp.proc_argv(os.getpid())
    assert argv and argv[0]


def test_live_proc_path_follows_exec(dp):
    """Review regression: exec keeps pid and start time but replaces the executable. A path cache keyed by
    (pid, start) returned the pre-exec path forever, so the recorded identity was the shim's Python, and a cache
    flush (after 20000 processes) made every running job look like a different process."""
    p = subprocess.Popen(["/bin/sh", "-c", "sleep 0.5; exec /bin/sleep 3"])
    try:
        time.sleep(0.2)
        before = dp.proc(p.pid)
        assert before.path in ("/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh")   # macOS /bin/sh execs a shell
        for _ in range(40):
            time.sleep(0.1)
            after = dp.proc(p.pid)
            if after.path == "/bin/sleep":
                break
        assert after.path == "/bin/sleep"
        assert (after.start_sec, after.start_usec) == (before.start_sec, before.start_usec)
        assert dp.identity_matches(JobIdentity(p.pid, after.start_sec, after.start_usec, "/bin/sleep", after.pgid))
        assert not dp.identity_matches(JobIdentity(p.pid, after.start_sec, after.start_usec, before.path, after.pgid))
    finally:
        p.kill()
        p.wait()


@pytest.mark.live
def test_live_resident_bytes_populated():
    import os
    from gsched.platform import get_platform
    p = get_platform().proc(os.getpid())
    assert p.resident_bytes and p.resident_bytes > 0
