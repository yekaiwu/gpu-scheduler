import os

from tests.daemon.conftest import client, run_ticks, spec


def test_submit_and_run_to_done(daemon, clock, plat):
    r = client(daemon, "submit", spec=spec())
    jid = r["job"]
    assert r["state"] == "queued"
    run_ticks(daemon, clock, 1)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "held", v
    w = plat.workload(jid)
    run_ticks(daemon, clock, 2)
    assert client(daemon, "get", job=jid)["held_reason"] == "live"
    w.exit(0)
    run_ticks(daemon, clock, 2)
    v = client(daemon, "get", job=jid)
    assert v["state"] == "done" and v["exit"]["code"] == 0
