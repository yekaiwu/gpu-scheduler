import pytest

from gsched.platform.fake import SIGKILL, SIGTERM, SIGUSR1
from tests.daemon.conftest import ClientError, client, project, run_ticks, spec


def go(d, clock, **over):
    jid = client(d, "submit", spec=spec(**over))["job"]
    run_ticks(d, clock, 2)
    return jid, d.platform.workload(jid)


def test_cancel_queued(daemon, clock):
    a = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    b = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    r = client(daemon, "cancel", job=b)
    assert r["state"] == "cancelled" and r["action"] == "cancelled"
    with pytest.raises(ClientError) as e:
        client(daemon, "cancel", job=b)
    assert e.value.code == "bad_state"


def test_cancel_atomic_is_flag_only(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="atomic")
    r = client(daemon, "cancel", job=jid)
    assert r["action"] == "cancel_requested_no_signal" and "terminate=true" in r["message"]
    run_ticks(daemon, clock, 5)
    assert plat.signals == []
    v = client(daemon, "get", job=jid)
    assert v["state"] == "held" and v["flags"]["cancel_requested"]
    w.exit(0)
    run_ticks(daemon, clock, 2)
    assert client(daemon, "get", job=jid)["state"] == "cancelled"


def test_cancel_terminate_atomic_kills_group(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="atomic")
    r = client(daemon, "cancel", job=jid, terminate=True)
    assert r["action"] == "terminating"
    assert any(s[1] == SIGTERM and s[0] < 0 for s in plat.signals)       # group signal
    run_ticks(daemon, clock, 3)
    assert client(daemon, "get", job=jid)["state"] == "cancelled"


def test_terminate_escalates_to_kill_after_kill_grace(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="atomic")
    w.on_signal(SIGTERM, "ignore")
    client(daemon, "cancel", job=jid, terminate=True)
    run_ticks(daemon, clock, 14)
    assert not any(s[1] == SIGKILL for s in plat.signals)
    run_ticks(daemon, clock, 3)
    kills = [s for s in plat.signals if s[1] == SIGKILL]
    assert len(kills) == 1
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "cancelled"        # shim died with the group: finalized from the kill
    assert len([s for s in plat.signals if s[1] == SIGKILL]) == 1


def test_cancel_restartable_term_kill(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="restartable")
    r = client(daemon, "cancel", job=jid)
    assert r["action"] == "stopping"
    run_ticks(daemon, clock, 3)
    assert client(daemon, "get", job=jid)["state"] == "cancelled"


def test_cancel_cooperative_requests_checkpoint_and_keeps_dir(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="cooperative")
    w.on_signal(SIGUSR1, "checkpoint_and_exit")
    r = client(daemon, "cancel", job=jid)
    assert r["action"] == "checkpoint_requested"
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "cancelled"
    import os
    assert os.path.exists(os.path.join(daemon.store.get(jid)["checkpoint_dir"], "READY"))
    assert [s[1] for s in plat.signals] == [SIGUSR1]


def test_cancel_cooperative_terminate(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="cooperative")
    r = client(daemon, "cancel", job=jid, terminate=True)
    assert r["action"] == "terminating"
    assert any(s[1] == SIGTERM for s in plat.signals)


def test_cancel_spawning_job_never_execs(daemon, clock, plat):
    plat.suppress_report = True
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 1)
    r = client(daemon, "cancel", job=jid)
    assert r["action"] == "cancelling"
    plat.suppress_report = False
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "cancelled" and v["exit"]["end_reason"] == "cancelled_before_start"
    assert not plat.workload(jid).released
    assert plat.signals == []


def test_mcp_terminate_forbidden_by_default(daemon, clock, plat):
    jid, w = go(daemon, clock, checkpoint="atomic")
    with pytest.raises(ClientError) as e:
        client(daemon, "cancel", door="mcp", job=jid, terminate=True)
    assert e.value.code == "forbidden"
    project(daemon, "p", mcp_terminate=True)
    assert client(daemon, "cancel", door="mcp", job=jid, terminate=True)["action"] == "terminating"


def test_unknown_job(daemon):
    with pytest.raises(ClientError) as e:
        client(daemon, "get", job="20260101T000000-0000")
    assert e.value.code == "unknown_job"
