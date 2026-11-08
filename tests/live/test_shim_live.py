import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.daemon.conftest import client
from tests.live.conftest import PY, add_project, drive, live_spec

pytestmark = pytest.mark.live


def terminal(d, jid):
    return lambda: d.store.get(jid)["state"] in ("done", "failed", "cancelled", "lost")


def test_live_shim_records_exit_code(live_daemon):
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec(["/bin/sh", "-c", "exit 7"]))["job"]
    drive(d, terminal(d, jid))
    r = d.store.get(jid)
    assert r["state"] == "failed" and r["exit_code"] == 7 and r["end_reason"] == "exit"
    assert os.path.exists(os.path.join(d.paths.job_dir(jid), "exit.1.json"))


def test_live_shim_survives_group_sigterm_and_records_signal(live_daemon):
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec(["/bin/sleep", "30"]))["job"]
    drive(d, lambda: d.store.get(jid)["held_reason"] == "live")
    r = d.store.get(jid)
    shim = r["shim_pid"]
    assert d.platform.pid_alive(shim)
    out = client(d, "cancel", job=jid, terminate=True)
    assert out["action"] == "terminating"
    drive(d, terminal(d, jid))
    r = d.store.get(jid)
    assert r["state"] == "cancelled" and r["exit_signal"] == signal.SIGTERM
    rec = json.load(open(os.path.join(d.paths.job_dir(jid), "exit.1.json")))
    assert rec["signal"] == signal.SIGTERM        # the shim survived SIGTERM to its group and wrote the record


def test_live_gate_close_exits_111_without_exec(tmp_path):
    marker = tmp_path / "ran"
    exit_file = tmp_path / "exit.json"
    gr, gw = os.pipe()
    rr, rw = os.pipe()
    p = subprocess.Popen([PY, "-I", "-m", "gsched.shim", "--job", "j", "--gate-fd", str(gr), "--report-fd", str(rw),
                          "--exit-file", str(exit_file), "--", "/usr/bin/touch", str(marker)],
                         pass_fds=(gr, rw), start_new_session=True)
    os.close(gr)
    os.close(rw)
    pid = int(os.read(rr, 64).strip())
    assert pid > 0
    os.close(gw)                                    # close WITHOUT writing
    assert p.wait(timeout=10) == 111
    assert not marker.exists()
    assert json.load(open(exit_file))["exit_code"] == 111


def test_live_gate_release_execs(tmp_path):
    marker = tmp_path / "ran"
    exit_file = tmp_path / "exit.json"
    gr, gw = os.pipe()
    rr, rw = os.pipe()
    p = subprocess.Popen([PY, "-I", "-m", "gsched.shim", "--job", "j", "--gate-fd", str(gr), "--report-fd", str(rw),
                          "--exit-file", str(exit_file), "--", "/usr/bin/touch", str(marker)],
                         pass_fds=(gr, rw), start_new_session=True)
    os.close(gr)
    os.close(rw)
    os.read(rr, 64)
    os.write(gw, b"1")
    assert p.wait(timeout=10) == 0
    assert marker.exists() and json.load(open(exit_file))["exit_code"] == 0


def test_live_reconcile_after_daemon_restart(live_daemon):
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec(["/bin/sleep", "3"]))["job"]
    drive(d, lambda: d.store.get(jid)["held_reason"] == "live")
    d.stop()
    d2 = live_daemon()
    assert d2.store.get(jid)["state"] == "held" and d2.reconcile_summary["kept"] == 1
    drive(d2, terminal(d2, jid), timeout=15)
    r = d2.store.get(jid)
    assert r["state"] == "done" and r["exit_code"] == 0


def test_live_peak_footprint_recorded(live_daemon):
    d = live_daemon()
    add_project(d)
    code = "b=bytearray(150*1024*1024); b[::4096]=b'x'*len(b[::4096]); import time; time.sleep(1.5)"
    jid = client(d, "submit", spec=live_spec([PY, "-c", code]))["job"]
    drive(d, terminal(d, jid))
    r = d.store.get(jid)
    assert r["state"] == "done"
    assert r["peak_footprint_bytes"] and r["peak_footprint_bytes"] > 100 * 2**20
    h = d.store.conn.execute("select peak_footprint_bytes from history where job=?", (jid,)).fetchone()
    assert h[0] > 100 * 2**20
    rec = json.load(open(os.path.join(d.paths.job_dir(jid), "exit.1.json")))
    print("shim-recorded peak:", rec.get("peak_footprint_bytes"))     # which case applies on this macOS


def test_live_daemon_end_to_end(live_daemon):
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec([PY, "-c", "print('hi')"]))["job"]
    drive(d, terminal(d, jid))
    assert d.store.get(jid)["state"] == "done"
    assert "hi" in client(d, "logs", job=jid)["data"]


def test_live_two_none_jobs_corun(live_daemon):
    d = live_daemon()
    add_project(d)
    a = client(d, "submit", spec=live_spec(["/bin/sleep", "2"]))["job"]
    b = client(d, "submit", spec=live_spec(["/bin/sleep", "2"]))["job"]
    drive(d, lambda: d.store.get(a)["held_reason"] == "live" and d.store.get(b)["held_reason"] == "live")
    drive(d, lambda: terminal(d, a)() and terminal(d, b)(), timeout=15)


def test_live_stale_pidfile_and_socket_replaced(live_daemon):
    d = live_daemon()
    d.stop()
    d2 = live_daemon()
    assert client(d2, "ping")["pid"] == os.getpid()


# ---- review regressions (2026-11-08) ---------------------------------------------------------------------------------
def test_live_wrapper_exec_after_refresh_is_not_lost(live_daemon):
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec(["/bin/sh", "-c", "sleep 2.5; exec /bin/sleep 2"]))["job"]
    drive(d, lambda: (d.store.get(jid)["pid_path"] or "").startswith("/bin/") and
          d.store.get(jid)["pid_path"] != "/bin/sleep", timeout=10)      # the shell (macOS /bin/sh execs bash)
    drive(d, lambda: d.store.get(jid)["pid_path"] == "/bin/sleep" or d.store.get(jid)["state"] != "held", timeout=10)
    assert d.store.get(jid)["state"] == "held"
    drive(d, terminal(d, jid), timeout=10)
    assert d.store.get(jid)["state"] == "done"
    assert [e for e in d.store.events(kinds=["pid_path_changed"]) if e["job"] == jid]


def test_live_job_runs_with_default_sigpipe(live_daemon):
    """The shim is Python, which ignores SIGPIPE; the workload must not inherit that across exec."""
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec(["/bin/sh", "-c", "yes | head -1 >/dev/null"]))["job"]
    drive(d, terminal(d, jid))
    err = open(os.path.join(d.paths.job_dir(jid), "stderr.log")).read()
    assert "Broken pipe" not in err and d.store.get(jid)["state"] == "done"


def test_live_shim_killed_workload_keeps_running(live_daemon):
    d = live_daemon()
    add_project(d)
    jid = client(d, "submit", spec=live_spec(["/bin/sleep", "4"]))["job"]
    drive(d, lambda: d.store.get(jid)["held_reason"] == "live")
    r = d.store.get(jid)
    os.kill(r["shim_pid"], signal.SIGKILL)          # our own test's shim (spawned by this daemon)
    t = time.monotonic()
    while time.monotonic() - t < 2.0:
        d.step(0.05)
    r = d.store.get(jid)
    assert r["state"] == "held" and r["shim_pid"] is None and d.platform.pid_alive(r["pid"])
    drive(d, terminal(d, jid), timeout=10)
    assert d.store.get(jid)["state"] == "lost"       # exit status is unknown without the shim


def test_live_workload_that_calls_setsid_is_still_stoppable(live_daemon):
    d = live_daemon()
    add_project(d)
    code = "import os,time\nos.setsid()\ntime.sleep(60)\n"
    jid = client(d, "submit", spec=live_spec([PY, "-c", code], checkpoint="restartable"))["job"]
    drive(d, lambda: d.store.get(jid)["held_reason"] == "live")
    pid = d.store.get(jid)["pid"]
    drive(d, lambda: d.last_scan is not None and pid in d.last_scan.by_pid and d.last_scan.by_pid[pid].pgid == pid)
    client(d, "cancel", job=jid)
    drive(d, terminal(d, jid), timeout=10)
    assert d.store.get(jid)["state"] == "cancelled" and not d.platform.pid_alive(pid)
