"""Daemon side of `gsched exec` (docs/implementation/16 section 1)."""

import pytest

from gsched.config import Config
from gsched.scan import Scanner
from tests.daemon.conftest import ClientError, Pending, client, run_ticks
from tests.daemon.test_external import admit_external, ext_spec


def test_cancel_end_reason_max_wait_on_queued_job_logs_exec_max_wait(daemon, clock):
    jid = client(daemon, "submit", spec=ext_spec())["job"]
    out = client(daemon, "cancel", job=jid, end_reason="max_wait")
    assert out["state"] == "cancelled"
    v = client(daemon, "get", job=jid)
    assert v["exit"]["end_reason"] == "max_wait"
    ev = daemon.store.events(kinds=["exec_max_wait"], since_ms=0, job=jid)
    assert len(ev) == 1 and ev[0]["body"]["waited_s"] == 0


def test_cancel_end_reason_max_wait_when_lease_was_just_issued(daemon, clock):
    jid, lease = admit_external(daemon, clock)                  # admitted, not attached: the wrapper timed out meanwhile
    out = client(daemon, "cancel", job=jid, end_reason="max_wait")
    assert out["state"] == "cancelled"
    assert client(daemon, "get", job=jid)["exit"]["end_reason"] == "max_wait"
    assert daemon.store.events(kinds=["exec_max_wait"], since_ms=0, job=jid)


def test_cancel_rejects_unknown_end_reason(daemon):
    jid = client(daemon, "submit", spec=ext_spec())["job"]
    with pytest.raises(ClientError) as e:
        client(daemon, "cancel", job=jid, end_reason="because")
    assert e.value.code == "invalid" and e.value.err["detail"]["field"] == "end_reason"


def test_plain_cancel_has_no_exec_event(daemon):
    jid = client(daemon, "submit", spec=ext_spec())["job"]
    client(daemon, "cancel", job=jid)
    assert not daemon.store.events(kinds=["exec_max_wait"], since_ms=0)
    assert client(daemon, "get", job=jid)["exit"]["end_reason"] == "cancelled"


def test_exec_external_tree_is_descendants_not_pgid(plat):
    """An external job's footprint is the attached pid and its descendants. Unrelated processes that merely share the
    pid's process group (a launchd job's other members) are not counted and not 'ours'."""
    wrapper = plat.add_foreign("wrapper", 0.1)                  # the wrapper (pgid == its own pid)
    pgid = plat.proc(wrapper).pgid
    cmd = plat.add_foreign("cmd", 3.0)
    plat._procs[cmd].ppid, plat._procs[cmd].pgid = wrapper, pgid          # the command: a child, same group
    grandchild = plat.add_foreign("worker", 2.0)
    plat._procs[grandchild].ppid, plat._procs[grandchild].pgid = cmd, pgid
    sibling = plat.add_foreign("unrelated-agent-member", 5.0)
    plat._procs[sibling].ppid, plat._procs[sibling].pgid = 1, pgid         # same pgid, not a descendant
    p = plat.proc(cmd)
    row = {"id": "j", "pid": cmd, "pgid": pgid, "shim_pid": None, "spawn": "external", "listen_port": None}
    r = Scanner(plat).classify(plat.processes(), [row], Config.defaults(), 0.0)
    assert r.trees["j"] == frozenset({cmd, grandchild})
    assert r.job_footprint["j"] == int(5.0 * 2**30)
    assert sibling not in r.ours and wrapper not in r.trees["j"]


def test_external_finish_just_after_pid_exit_is_not_lost(daemon, clock, plat):
    """`gsched exec` reports the exit milliseconds after its child dies; a tick that lands in between must not win."""
    jid, lease = admit_external(daemon, clock)
    pid = plat.add_foreign("child", 1)
    client(daemon, "attach", lease=lease, pid=pid)
    plat.remove(pid)
    run_ticks(daemon, clock, 1)                                  # the pid is seen gone for the first time
    assert client(daemon, "get", job=jid)["state"] == "held"
    assert client(daemon, "finish", lease=lease, exit_code=7)["state"] == "failed"
    v = client(daemon, "get", job=jid)
    assert v["exit"]["code"] == 7 and v["exit"]["end_reason"] == "exit"
