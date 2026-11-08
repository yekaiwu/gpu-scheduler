"""`gsched exec` (docs/implementation/16 section 1). The daemon is a scripted socket (tests/cli/conftest.py); the
wrapped commands are real processes, so exit codes and signals are the real thing."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JOB = "20261108T231500-7f3a"
LEASE = "ab" * 32
BASE = ["exec", "--project", "p", "--mem", "2", "--gpu", "none", "--checkpoint", "atomic", "--est", "1h"]


class Execd(BaseException):
    """Stands in for os.execvp, which never returns."""

    def __init__(self, argv):
        self.argv = argv


@pytest.fixture
def execvp(monkeypatch):
    calls = []

    def fake(file, argv):
        calls.append((file, list(argv)))
        raise Execd(list(argv))
    monkeypatch.setattr(os, "execvp", fake)
    return calls


def script_admitted(fake, finish_log=None):
    fake.reply("submit", {"job": JOB, "state": "queued", "queue": "day", "warnings": []})
    fake.reply("wait", {"job": JOB, "state": "held", "held_reason": "await_attach", "lease": LEASE})
    fake.reply("attach", {"job": JOB, "state": "held", "held_reason": "live"})
    fake.reply("finish", {"job": JOB, "state": "done"})
    fake.reply("release", {"job": JOB, "state": "done"})
    fake.reply("cancel", {"job": JOB, "state": "cancelled", "action": "cancelled"})


def run_wrapper(fake, argv, **kw):
    env = dict(os.environ, GSCHED_SOCKET=fake.path, GSCHED_HOME=str(Path(fake.path).parent / "home"))
    env.pop("GSCHED_PROJECT", None)
    return subprocess.Popen([sys.executable, "-m", "gsched", *BASE, *argv], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


def methods(fake):
    return [r["method"] for r in fake.requests]


def test_exec_failopen_when_daemon_down_execs_directly(short_tmp, monkeypatch, cli, execvp):
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "nobody.sock"))
    monkeypatch.setenv("GSCHED_HOME", str(short_tmp / "home"))
    with pytest.raises(Execd) as e:
        cli(*BASE, "--connect-grace", "0", "--", "echo", "hi")
    assert e.value.argv == ["echo", "hi"] and execvp == [("echo", ["echo", "hi"])]


def test_exec_observe_mode_execs_directly(fake, cli, execvp, capsys):
    fake.reply("submit", {"state": "observe", "job": None, "would": {"would": "admit_now"}, "hint": "observe"})
    with pytest.raises(Execd):
        cli(*BASE, "--", "echo", "hi")
    assert methods(fake) == ["submit"]                                # nothing queued, nobody waited on


def test_exec_failclosed_exit_75(fake, short_tmp, monkeypatch, cli, execvp):
    fake.reply("submit", {"state": "observe", "job": None, "would": {}, "hint": "observe"})
    rc, out, err = cli(*BASE, "--fail-closed", "--", "echo", "hi")
    assert rc == 75 and execvp == [] and "fail-closed" in err
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "nobody.sock"))      # daemon down
    rc, out, err = cli(*BASE, "--fail-closed", "--connect-grace", "0", "--", "echo", "hi")
    assert rc == 75 and execvp == []


def test_exec_request_error_falls_back_loudly(fake, cli, execvp):
    fake.reply("submit", error={"code": "unknown_project", "message": "no project p"})
    with pytest.raises(Execd):
        cli(*BASE, "--", "echo", "hi")
    fake.reply("submit", error={"code": "too_big", "message": "needs 900 GiB"})
    rc, out, err = cli(*BASE, "--fail-closed", "--", "echo", "hi")
    assert rc == 2 and "too_big" in err


def test_exec_fallback_message_on_stderr(fake, cli, execvp, capsys):
    fake.reply("submit", error={"code": "unknown_project", "message": "no project p"})
    with pytest.raises(Execd):
        cli(*BASE, "--", "echo", "hi")
    assert "gsched: exec fallback: unknown_project: no project p" in capsys.readouterr().err


def test_exec_propagates_exit_code(fake):
    script_admitted(fake)
    p = run_wrapper(fake, ["--", "sh", "-c", "echo hi; exit 7"])
    out, err = p.communicate(timeout=30)
    assert p.returncode == 7 and out == "hi\n"                        # stdio inherited, status exact
    assert methods(fake) == ["submit", "wait", "attach", "finish"]
    sub = fake.requests[0]["params"]["spec"]
    assert sub["spawn"] == "external" and sub["cwd"] == str(ROOT) and "env" not in sub
    assert sub["command"] == ["sh", "-c", "echo hi; exit 7"] and sub["checkpoint"] == "atomic"
    assert sub["resources"] == {"memory_gib": 2.0, "gpu": "none", "cpu_cores": 1}
    assert fake.requests[1]["params"] == {"job": JOB, "until": "start"} and fake.requests[1]["door"] == "launcher"
    assert fake.requests[2]["params"]["lease"] == LEASE and fake.requests[3]["params"] == {"lease": LEASE, "exit_code": 7}


def test_exec_keeps_session_but_child_leads_own_group(fake, tmp_path):
    """Changed after live testing (QUESTIONS #37): same session as the wrapper, but the child leads its own
    process group, so a group-wide signal to the wrapper is not delivered twice (directly + forwarded)."""
    script_admitted(fake)
    marker = tmp_path / "ids"
    p = run_wrapper(fake, ["--", "sh", "-c",
                           f"echo $$ $(ps -o pgid= -p $$) $PPID $(ps -o pgid= -p $PPID) "
                           f"$(ps -o sess= -p $$) $(ps -o sess= -p $PPID) > {marker}"])
    p.communicate(timeout=30)
    child, child_pg, parent, parent_pg, child_sess, parent_sess = marker.read_text().split()
    assert int(parent) == p.pid                  # the command is a direct child of the wrapper
    assert int(child_pg) == int(child) != int(parent_pg)   # it leads its own process group
    assert child_sess == parent_sess             # but stays in the wrapper's session


def test_exec_propagates_signal_death(fake):
    script_admitted(fake)
    p = run_wrapper(fake, ["--", "sh", "-c", "kill -9 $$"])
    p.communicate(timeout=30)
    assert p.returncode == -signal.SIGKILL                            # re-raised on itself, not exit 137
    fin = [r for r in fake.requests if r["method"] == "finish"][0]["params"]
    assert fin == {"lease": LEASE, "signal": 9}


def test_exec_sigterm_while_queued_cancels_and_never_runs(fake, tmp_path):
    fake.reply("submit", {"job": JOB, "state": "queued", "queue": "day", "warnings": []})
    gate = threading.Event()
    fake.delay["wait"] = gate                                         # the wait is parked, no lease ever arrives
    fake.reply("wait", {"job": JOB, "state": "cancelled"})
    fake.reply("cancel", {"job": JOB, "state": "cancelled", "action": "cancelled"})
    marker = tmp_path / "ran"
    p = run_wrapper(fake, ["--", "sh", "-c", f"touch {marker}"])
    for _ in range(200):
        if "wait" in methods(fake):
            break
        time.sleep(0.05)
    assert "wait" in methods(fake)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=30)
    gate.set()
    assert p.returncode == 143 and not marker.exists()
    assert "cancel" in methods(fake) and "attach" not in methods(fake)
    cancel = [r for r in fake.requests if r["method"] == "cancel"][0]["params"]
    assert cancel == {"job": JOB}


@pytest.mark.parametrize("sig,code", [(signal.SIGINT, 130), (signal.SIGHUP, 129)])
def test_exec_other_terminating_signals_while_queued(fake, tmp_path, sig, code):
    fake.reply("submit", {"job": JOB, "state": "queued", "queue": "day", "warnings": []})
    gate = threading.Event()
    fake.delay["wait"] = gate
    fake.reply("wait", {"job": JOB, "state": "cancelled"})
    fake.reply("cancel", {"job": JOB, "state": "cancelled", "action": "cancelled"})
    p = run_wrapper(fake, ["--", "true"])
    for _ in range(200):
        if "wait" in methods(fake):
            break
        time.sleep(0.05)
    p.send_signal(sig)
    p.communicate(timeout=30)
    gate.set()
    assert p.returncode == code and "cancel" in methods(fake)


def queued_forever(fake):
    fake.reply("submit", {"job": JOB, "state": "queued", "queue": "day", "warnings": []})
    gate = threading.Event()
    fake.delay["wait"] = gate
    fake.reply("wait", {"job": JOB, "state": "queued"})
    fake.reply("cancel", {"job": JOB, "state": "cancelled", "action": "cancelled"})
    return gate


def test_exec_max_wait_run(fake, tmp_path):
    gate = queued_forever(fake)
    marker = tmp_path / "ran"
    p = run_wrapper(fake, ["--max-wait", "1", "--", "sh", "-c", f"echo direct > {marker}; exit 3"])
    out, err = p.communicate(timeout=30)
    gate.set()
    assert p.returncode == 3 and marker.read_text() == "direct\n"      # exec'd directly after the cancel
    cancel = [r for r in fake.requests if r["method"] == "cancel"][0]["params"]
    assert cancel == {"job": JOB, "end_reason": "max_wait"}
    assert "max-wait" in err and "running the command directly" in err
    assert "attach" not in methods(fake)


def test_exec_max_wait_fail(fake, tmp_path):
    gate = queued_forever(fake)
    marker = tmp_path / "ran"
    p = run_wrapper(fake, ["--max-wait", "1", "--on-max-wait", "fail", "--", "sh", "-c", f"touch {marker}"])
    out, err = p.communicate(timeout=30)
    gate.set()
    assert p.returncode == 75 and not marker.exists()
    assert "not running the command" in err
    assert [r for r in fake.requests if r["method"] == "cancel"][0]["params"]["end_reason"] == "max_wait"


def test_exec_forwards_sigterm_to_child(fake, tmp_path):
    script_admitted(fake)
    started, got = tmp_path / "started", tmp_path / "got"
    code = (f"import signal,sys,time,pathlib\n"
            f"signal.signal(signal.SIGTERM, lambda s,f: (pathlib.Path('{got}').write_text('TERM'), sys.exit(5)))\n"
            f"pathlib.Path('{started}').write_text('1')\n"
            f"time.sleep(30)\n")
    p = run_wrapper(fake, ["--", sys.executable, "-c", code])
    for _ in range(200):
        if started.exists():
            break
        time.sleep(0.05)
    assert started.exists()
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=30)
    assert got.read_text() == "TERM"
    assert p.returncode == 5                                            # the child's own status, not 143
    assert [r for r in fake.requests if r["method"] == "finish"][0]["params"]["exit_code"] == 5


def test_exec_survives_daemon_loss_after_attach(fake):
    script_admitted(fake)
    fake.script.pop("finish")                                           # finish now fails: unscripted -> invalid
    p = run_wrapper(fake, ["--", "sh", "-c", "exit 4"])
    out, err = p.communicate(timeout=30)
    assert p.returncode == 4 and "marked lost" in err                   # exits with the child's status anyway
    assert methods(fake).count("finish") == 3                           # retried three times


def test_exec_cancelled_while_queued_does_not_run(fake, tmp_path):
    fake.reply("submit", {"job": JOB, "state": "queued", "queue": "day", "warnings": []})
    fake.reply("wait", {"job": JOB, "state": "cancelled", "exit": {"end_reason": "cancelled"}})
    marker = tmp_path / "ran"
    p = run_wrapper(fake, ["--", "sh", "-c", f"touch {marker}"])
    out, err = p.communicate(timeout=30)
    assert p.returncode == 2 and not marker.exists() and "was not run" in err


def test_exec_requires_mem_and_checkpoint(cli, fake):
    rc, out, err = cli("exec", "--project", "p", "--checkpoint", "atomic", "--", "true")
    assert rc == 64 and "--mem" in err
    rc, out, err = cli("exec", "--project", "p", "--mem", "2", "--", "true")
    assert rc == 64 and "--checkpoint" in err
    rc, out, err = cli("exec", "--project", "p", "--mem", "2", "--checkpoint", "atomic")
    assert rc == 64
    rc, out, err = cli("exec", "--mem", "2", "--checkpoint", "atomic", "--", "true")
    assert rc == 64 and "no project" in err


def test_exec_atomic_without_est_gets_the_configured_default(fake, cli, execvp):
    fake.reply("submit", {"state": "observe", "job": None, "would": {}, "hint": "x"})
    with pytest.raises(Execd):
        cli("exec", "--project", "p", "--mem", "2", "--checkpoint", "atomic", "--", "true")
    assert fake.requests[0]["params"]["spec"]["runtime"] == {"estimate_s": 7200}
    with pytest.raises(Execd):
        cli("exec", "--project", "p", "--mem", "2", "--checkpoint", "restartable", "--", "true")
    assert "runtime" not in fake.requests[1]["params"]["spec"]


def test_exec_fail_open_and_closed_are_exclusive(cli, fake):
    rc, out, err = cli(*BASE, "--fail-open", "--fail-closed", "--", "true")
    assert rc == 64


def test_exec_help_does_not_import_the_wrapper():
    code = ("import sys; from gsched import cli; cli.main(['exec','--help']) if False else None; "
            "cli.build_parser(); assert 'gsched.exec_wrap' not in sys.modules and 'gsched.daemon' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def test_exec_child_leads_own_process_group_source():
    """Regression (live 2026-11-08): the exec child must not share the wrapper's process group, or a
    group-wide SIGTERM is delivered twice (directly + forwarded) and llama-server aborts."""
    import inspect
    from gsched import exec_wrap, launcher
    assert "own_group=True" in inspect.getsource(exec_wrap)
    assert "process_group=0 if (own_group and not new_session)" in inspect.getsource(launcher.Lease.spawn)



def test_exec_connect_grace_retries_a_restarting_daemon(fake, cli, execvp, monkeypatch):
    """Regression (live 2026-11-08): a wrapper started while the daemon was restarting failed open at once and ran
    llama-server unscheduled. A refused/missing socket is now retried for --connect-grace before failing open."""
    from gsched import client as gclient
    real = gclient.call
    calls = {"n": 0}

    def flaky(method, params, **kw):
        if method == "submit" and calls["n"] < 2:
            calls["n"] += 1
            raise gclient.ConnectError("socket missing")
        return real(method, params, **kw)
    monkeypatch.setattr(gclient, "call", flaky)
    fake.reply("submit", {"state": "observe", "job": None, "would": {}, "hint": "observe"})
    with pytest.raises(Execd):
        cli(*BASE, "--connect-grace", "5", "--", "echo", "hi")
    assert calls["n"] == 2 and methods(fake) == ["submit"]       # retried twice, then reached the daemon


def test_exec_connect_grace_expires_then_fails_open(short_tmp, monkeypatch, cli, execvp):
    import time as _t
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "nobody.sock"))
    monkeypatch.setenv("GSCHED_HOME", str(short_tmp / "home"))
    t0 = _t.monotonic()
    with pytest.raises(Execd):
        cli(*BASE, "--connect-grace", "1", "--", "echo", "hi")
    assert _t.monotonic() - t0 >= 1.0


def test_exec_replace_attaches_own_pid_then_execs_in_place(fake, cli, execvp):
    """--replace (live 2026-11-08): keep pid/ancestry/TCC attribution of the LaunchAgent's process."""
    script_admitted(fake)
    with pytest.raises(Execd) as e:
        cli(*BASE, "--replace", "--", "echo", "hi")
    assert e.value.argv == ["echo", "hi"]
    att = [r for r in fake.requests if r["method"] == "attach"]
    assert len(att) == 1 and att[0]["params"] == {"lease": LEASE, "pid": os.getpid()}
    assert "finish" not in methods(fake)                     # nobody is left to report the exit


def test_exec_replace_attach_failure_fails_open(fake, cli, execvp):
    script_admitted(fake)
    fake.reply("attach", error={"code": "bad_state", "message": "nope"})
    with pytest.raises(Execd):
        cli(*BASE, "--replace", "--", "echo", "hi")
    assert "release" in methods(fake)
