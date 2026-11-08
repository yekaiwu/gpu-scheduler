import os

import pytest

from tests.daemon.conftest import ClientError, Pending, client, run_ticks, spec


def ext_spec(**over):
    s = spec(spawn="external", **over)
    del s["command"]
    return s


def wait_start(d, jid):
    p = Pending(d, "wait", job=jid, until="start")
    return p


def test_external_not_started_without_waiter(daemon, clock):
    jid = client(daemon, "submit", spec=ext_spec())["job"]
    run_ticks(daemon, clock, 3)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "queued" and v["blocked_by"] == ["no_start_waiter"] and "wait" in v["why"]


def test_external_wait_start_returns_lease_once(daemon, clock):
    jid = client(daemon, "submit", spec=ext_spec())["job"]
    p = wait_start(daemon, jid)
    for _ in range(5):
        r = p.poll()
        if r:
            break
    assert r["ok"] and r["result"]["held_reason"] == "await_attach"
    lease = r["result"]["lease"]
    assert len(lease) == 64
    # later calls never reveal the lease again
    r2 = client(daemon, "wait", job=jid, until="start")
    assert "lease" not in r2
    assert lease not in str(client(daemon, "get", job=jid))
    assert lease not in str(client(daemon, "status"))
    assert lease not in str(client(daemon, "list"))
    row = daemon.store.get(jid)
    assert row["lease_hash"] and lease not in row["lease_hash"]
    p.close()


def test_wait_start_on_daemon_job_invalid(daemon):
    jid = client(daemon, "submit", spec=spec())["job"]
    with pytest.raises(ClientError) as e:
        client(daemon, "wait", job=jid, until="start")
    assert e.value.code == "invalid"


def admit_external(d, clock):
    jid = client(d, "submit", spec=ext_spec())["job"]
    p = wait_start(d, jid)
    r = None
    for _ in range(5):
        r = p.poll()
        if r:
            break
    p.close()
    return jid, r["result"]["lease"]


def test_attach_finish_flow(daemon, clock, plat):
    jid, lease = admit_external(daemon, clock)
    pid = plat.add_foreign("child", 3)
    r = client(daemon, "attach", lease=lease, pid=pid)
    assert r == {"job": jid, "state": "held", "held_reason": "live"}
    run_ticks(daemon, clock, 2)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "held" and v["footprint_gib"] == 3.0
    # an external job is never signaled
    c = client(daemon, "cancel", job=jid)
    assert c["action"] == "cancel_requested_no_signal"
    with pytest.raises(ClientError) as e:
        client(daemon, "cancel", job=jid, terminate=True)
    assert e.value.code == "forbidden"
    assert plat.signals == []
    plat.remove(pid)
    f = client(daemon, "finish", lease=lease, exit_code=0)
    assert f["state"] == "cancelled"          # cancel_requested + any exit


def test_finish_idempotent(daemon, clock, plat):
    jid, lease = admit_external(daemon, clock)
    pid = plat.add_foreign("child", 1)
    client(daemon, "attach", lease=lease, pid=pid)
    assert client(daemon, "finish", lease=lease, exit_code=3) == {"job": jid, "state": "failed"}
    assert client(daemon, "finish", lease=lease, exit_code=3) == {"job": jid, "state": "failed"}
    assert client(daemon, "release", lease=lease) == {"job": jid, "state": "failed"}


def test_attach_rejects_pid_0_1_and_self(daemon, clock, plat):
    jid, lease = admit_external(daemon, clock)
    for pid in (0, 1, os.getpid(), -5, 99999999):
        with pytest.raises(ClientError) as e:
            client(daemon, "attach", lease=lease, pid=pid)
        assert e.value.code == "invalid"
    with pytest.raises(ClientError) as e:
        client(daemon, "attach", lease="0" * 64, pid=5)
    assert e.value.code == "bad_lease"
    other = plat.add_foreign("x", 1, uid=os.getuid() + 1)
    with pytest.raises(ClientError):
        client(daemon, "attach", lease=lease, pid=other)
    assert daemon.store.get(jid)["pid"] is None


def test_release_without_attach_cancels(daemon, clock):
    jid, lease = admit_external(daemon, clock)
    r = client(daemon, "release", lease=lease)
    assert r["state"] == "cancelled"
    assert daemon.store.get(jid)["end_reason"] == "released_unstarted"


def test_release_attached_alive_vs_gone(daemon, clock, plat):
    jid, lease = admit_external(daemon, clock)
    pid = plat.add_foreign("child", 1)
    client(daemon, "attach", lease=lease, pid=pid)
    with pytest.raises(ClientError) as e:
        client(daemon, "release", lease=lease)
    assert e.value.code == "still_running"
    plat.remove(pid)
    # the tick notices the vanished pid and marks the job lost
    run_ticks(daemon, clock, 2)
    assert client(daemon, "get", job=jid)["state"] == "lost"
    assert client(daemon, "release", lease=lease)["state"] == "lost"


def test_attach_timeout_frees_slot(daemon, clock):
    jid, lease = admit_external(daemon, clock)
    run_ticks(daemon, clock, 31)
    row = daemon.store.get(jid)
    assert row["state"] == "cancelled" and row["end_reason"] == "attach_timeout"


def test_cancel_by_lease_before_attach(daemon, clock):
    jid, lease = admit_external(daemon, clock)
    r = client(daemon, "cancel", lease=lease)
    assert r["state"] == "cancelled"


def test_parked_wait_start_cancels_after_grace_when_peer_dies(daemon, clock, plat):
    blocker = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    jid = client(daemon, "submit", spec=ext_spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    fake_client = plat.add_foreign("waiter", 0.1)
    plat.peer_pid = fake_client
    p = wait_start(daemon, jid)
    p.poll(3)
    assert daemon.start_waiters() == {jid}
    plat.remove(fake_client)                   # the waiter process dies without closing cleanly
    run_ticks(daemon, clock, 2)
    assert daemon.start_waiters() == set()
    assert daemon.store.get(jid)["state"] == "queued"
    run_ticks(daemon, clock, 31)
    row = daemon.store.get(jid)
    assert row["state"] == "cancelled" and row["end_reason"] == "waiter_gone"
    p.close()
    assert daemon.store.get(blocker)["state"] == "held"        # the blocker was never touched


def test_waiter_reparks_within_grace_keeps_job(daemon, clock, plat):
    blocker = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    jid = client(daemon, "submit", spec=ext_spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    p = wait_start(daemon, jid)
    p.poll(3)
    p.close()                                   # waiter goes away (socket closed)
    run_ticks(daemon, clock, 5)
    p2 = wait_start(daemon, jid)
    p2.poll(3)
    run_ticks(daemon, clock, 40)
    assert daemon.store.get(jid)["state"] == "queued"
    p2.close()


def test_wait_exit_peer_death_no_state_change(daemon, clock, plat):
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 2)
    fake_client = plat.add_foreign("waiter", 0.1)
    plat.peer_pid = fake_client
    p = Pending(daemon, "wait", job=jid)
    p.poll(3)
    assert any(c.parked for c in daemon.conns)
    plat.remove(fake_client)
    run_ticks(daemon, clock, 2)
    assert not any(c.parked for c in daemon.conns)
    assert daemon.store.get(jid)["state"] == "held"        # the job is untouched
    p.close()


def test_wait_exit_returns_on_finish(daemon, clock, plat):
    jid = client(daemon, "submit", spec=spec())["job"]
    run_ticks(daemon, clock, 2)
    p = Pending(daemon, "wait", job=jid)
    p.poll(3)
    assert p.resp is None
    plat.workload(jid).exit(0)
    run_ticks(daemon, clock, 2)
    r = p.poll(3)
    assert r["ok"] and r["result"]["state"] == "done"
    p.close()


def test_get_wait_returns_on_change(daemon, clock, plat):
    blocker = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    p = Pending(daemon, "get", job=jid, wait_s=20)
    p.poll(3)
    assert p.resp is None
    client(daemon, "cancel", job=jid)
    r = p.poll(5)
    assert r["result"]["state"] == "cancelled"
    p.close()


def test_get_wait_times_out(daemon, clock, plat):
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    p = Pending(daemon, "get", job=jid, wait_s=5)
    p.poll(3)
    assert p.resp is None
    run_ticks(daemon, clock, 6)
    r = p.poll(3)
    assert r["ok"] and r["result"]["state"] == "held"
    p.close()


def test_get_wait_capped_for_mcp(daemon, clock):
    jid = client(daemon, "submit", spec=spec(resources={"memory_gib": 2, "gpu": "exclusive"}))["job"]
    run_ticks(daemon, clock, 2)
    p = Pending(daemon, "get", door="mcp", job=jid, wait_s=100)
    p.poll(3)
    parked = [c.parked for c in daemon.conns if c.parked]
    assert parked and parked[0].deadline_mono - clock.mono() <= 30.0
    q = Pending(daemon, "get", door="cli", job=jid, wait_s=1000)
    q.poll(3)
    parked = [c.parked for c in daemon.conns if c.parked and c.door == "cli"]
    assert 100 < parked[0].deadline_mono - clock.mono() <= 300.0
    p.close()
    q.close()
