"""Live checks for PR 12: the real shim chdir protocol, the real async helpers, and `gsched exec` against a real daemon.
Everything runs in /tmp; nothing touches ~/Documents, LaunchAgents or launchctl."""

import json
import os
import socket
import subprocess
import sys
import time

import pytest

from tests.daemon.conftest import client
from tests.live.conftest import PY, add_project, drive

pytestmark = pytest.mark.live
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _shim(tmp_path, cwd, command):
    exit_file = tmp_path / "exit.json"
    gr, gw = os.pipe()
    rr, rw = os.pipe()
    argv = [PY, "-I", "-m", "gsched.shim", "--job", "j", "--gate-fd", str(gr), "--report-fd", str(rw),
            "--exit-file", str(exit_file)]
    if cwd:
        argv += ["--cwd", str(cwd)]
    p = subprocess.Popen(argv + ["--"] + command, pass_fds=(gr, rw), start_new_session=True, cwd=str(tmp_path))
    os.close(gr)
    os.close(rw)
    return p, gw, rr, exit_file


def test_live_shim_chdirs_after_gate_and_reports_cwd_ok(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "pwd.txt"
    p, gw, rr, exit_file = _shim(tmp_path, work, ["/bin/sh", "-c", f"pwd -P > {out}"])
    first = os.read(rr, 64)
    assert first.strip().isdigit()                       # the pid line
    os.write(gw, b"1")
    buf = b""
    deadline = time.monotonic() + 10
    while b"cwd_ok" not in buf and time.monotonic() < deadline:
        chunk = os.read(rr, 64)
        if not chunk:
            break
        buf += chunk
    assert buf.strip() == b"cwd_ok"                      # the second line, written after chdir() succeeded
    assert p.wait(timeout=10) == 0
    assert out.read_text().strip() == os.path.realpath(work)
    assert json.load(open(exit_file))["exit_code"] == 0


def test_live_shim_bad_cwd_exits_126_without_running(tmp_path):
    marker = tmp_path / "ran"
    p, gw, rr, exit_file = _shim(tmp_path, tmp_path / "does-not-exist", ["/usr/bin/touch", str(marker)])
    os.read(rr, 64)
    os.write(gw, b"1")
    assert p.wait(timeout=10) == 126 and not marker.exists()
    assert json.load(open(exit_file))["exit_code"] == 126


def test_live_async_helpers_probe_kill_and_lsof(live_daemon, tmp_path):
    d = live_daemon()
    plat = d.platform
    ok = plat.spawn_async([PY, "-I", "-c", "import os,sys; os.listdir(sys.argv[1]); print('ok')", str(tmp_path)])
    res = None
    deadline = time.monotonic() + 10
    while res is None and time.monotonic() < deadline:
        res = ok.poll()
        time.sleep(0.02)
    assert res is not None and res[0] == 0 and "ok" in res[1]
    ok.close()
    hung = plat.spawn_async([PY, "-I", "-c", "import time; time.sleep(60)"])
    assert hung.poll() is None
    plat.kill_async(hung)                                  # SIGKILL to a helper this test started
    res = None
    deadline = time.monotonic() + 10
    while res is None and time.monotonic() < deadline:
        res = hung.poll()
        time.sleep(0.02)
    assert res is not None and res[0] != 0
    hung.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    lk = plat.port_owner_start(port)
    res = None
    deadline = time.monotonic() + 10
    while res is None and time.monotonic() < deadline:
        res = lk.poll()
        time.sleep(0.02)
    s.close()
    lk.close()
    assert res is not None and str(os.getpid()) in res[1].split()


def test_live_tcc_probe_of_unprotected_dir_is_ok(live_daemon, tmp_path):
    d = live_daemon()
    d.tcc.home = str(tmp_path)                             # a pretend home: roots are tmp_path/Documents etc.
    (tmp_path / "Documents").mkdir()
    d.cfg = d.cfg.replace(tcc_protected_roots=())
    d.tcc._cache_key = None
    d.tcc.entries.clear()
    d.tcc.start_all()
    deadline = time.monotonic() + 10
    root = str(tmp_path / "Documents")
    while d.tcc.state(root) == "unknown" and time.monotonic() < deadline:
        d.step(0.05)
    assert d.tcc.state(root) == "ok" and d.tcc.state(str(tmp_path / "Desktop")) in ("absent", "unknown", "ok")


def test_live_exec_wraps_real_command_end_to_end(live_daemon, tmp_path):
    d = live_daemon()
    add_project(d)
    env = dict(os.environ, GSCHED_SOCKET=str(d.socket_path), GSCHED_HOME=str(d.paths.home))
    env.pop("GSCHED_PROJECT", None)
    p = subprocess.Popen([PY, "-m", "gsched", "exec", "--project", "p", "--name", "wrapped", "--mem", "0.25",
                          "--gpu", "none", "--checkpoint", "atomic", "--est", "1m", "--",
                          "sh", "-c", "echo hi; exit 7"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    drive(d, lambda: p.poll() is not None, timeout=30)
    out, err = p.communicate()
    assert p.returncode == 7 and out == "hi\n", (out, err)
    jobs = client(d, "list", state=["done", "failed"])["jobs"]
    j = [x for x in jobs if x["name"] == "wrapped"][0]
    assert j["spawn"] == "external" and j["state"] == "failed" and j["exit"]["code"] == 7
    live_list = client(d, "list")["jobs"]
    assert any(x["job"] == j["job"] for x in live_list)       # `list` without a state filter shows it
    assert d.store.get(j["job"])["pid"] is not None
    assert d.platform.signals == [] if hasattr(d.platform, "signals") else True


@pytest.mark.live
def test_live_exec_group_sigterm_reaches_child_once(live_daemon, tmp_path):
    """Regression (live 2026-11-08): a SIGTERM to the wrapper's process group must reach the wrapped
    command exactly once. Sharing the group delivered it twice and llama-server aborted."""
    import signal as _signal
    d = live_daemon()
    add_project(d)
    env = dict(os.environ, GSCHED_SOCKET=str(d.socket_path), GSCHED_HOME=str(d.paths.home))
    env.pop("GSCHED_PROJECT", None)
    out_file = tmp_path / "count.txt"
    script = ("import signal,sys,time\nn=[0]\n"
              "def h(s,f): n[0]+=1\n"
              "signal.signal(signal.SIGTERM,h)\nopen(sys.argv[1]+'.ready','w').close()\n"
              "t=time.time()\nwhile time.time()-t<3: time.sleep(0.05)\n"
              "open(sys.argv[1],'w').write(str(n[0]))\n")
    p = subprocess.Popen([PY, "-m", "gsched", "exec", "--project", "p", "--name", "sigonce", "--mem", "0.1",
                          "--gpu", "none", "--checkpoint", "restartable", "--", PY, "-c", script, str(out_file)],
                         cwd=ROOT, env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    drive(d, lambda: (tmp_path / "count.txt.ready").exists(), timeout=20)
    os.killpg(p.pid, _signal.SIGTERM)          # group-wide, like Ctrl-C or a supervisor killing the group
    drive(d, lambda: p.poll() is not None, timeout=20)
    assert out_file.read_text() == "1", p.stderr.read()


@pytest.mark.live
def test_live_exec_replace_keeps_pid_and_is_tracked(live_daemon, tmp_path):
    d = live_daemon()
    add_project(d)
    env = dict(os.environ, GSCHED_SOCKET=str(d.socket_path), GSCHED_HOME=str(d.paths.home))
    env.pop("GSCHED_PROJECT", None)
    marker = tmp_path / "pid"
    p = subprocess.Popen([PY, "-m", "gsched", "exec", "--replace", "--project", "p", "--name", "inplace", "--mem", "0.1",
                          "--gpu", "none", "--checkpoint", "restartable", "--",
                          "sh", "-c", f"echo $$ > {marker}; sleep 1.5"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    drive(d, lambda: marker.exists() and marker.read_text().strip() != "", timeout=20)
    assert int(marker.read_text()) == p.pid                 # the command IS the launched process
    held = [x for x in client(d, "list")["jobs"] if x["name"] == "inplace"][0]
    assert held["state"] == "held" and d.store.get(held["job"])["pid"] == p.pid
    drive(d, lambda: p.poll() is not None, timeout=20)
    drive(d, lambda: d.store.get(held["job"])["state"] != "held", timeout=10)
    row = d.store.get(held["job"])
    assert row["state"] == "done" and row["end_reason"] == "exit_unobserved"   # in-place: exit not observable
