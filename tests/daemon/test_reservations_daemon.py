"""Time reservations end to end against the fake platform (docs/implementation/16 section 2)."""

import json

import pytest

from gsched import status as gstatus
from gsched.config import Config
from gsched.platform.fake import SIGTERM
from tests.daemon.conftest import client, project, run_ticks, spec
from tests.policy.builders import ms

R = {"name": "t233-pass", "days": ["mon", "tue", "wed", "thu", "fri"], "start": "19:00", "end": "23:30",
     "project": "latent-alpha", "memory_gib": 27, "gpu": "exclusive", "unload_restartable": True}


@pytest.fixture
def rdaemon(make_daemon, clock):
    cfg = Config.defaults().replace(enforce=True, timezone="UTC", night_window=None, reservations=(R,))
    d = make_daemon(cfg)
    project(d, "p")
    project(d, "latent-alpha")
    clock.set_wall(ms("2026-11-09T18:00:00Z"))          # Monday, one hour before the window
    return d


def restartable(**over):
    return spec(checkpoint="restartable", resources={"memory_gib": 20, "gpu": "exclusive"},
                runtime={"estimate_s": None}, **over)


def events(d, kind):
    return [e for e in d.store.events(kinds=[kind], since_ms=0)]


def test_reservation_start_unloads_other_restartable_once_per_day(rdaemon, clock, plat):
    d = rdaemon
    jid = client(d, "submit", spec=restartable())["job"]
    run_ticks(d, clock, 3)
    assert d.store.get(jid)["state"] == "held"                       # 18:00: no estimate, restartable: exempt
    run_ticks(d, clock, 3590)                                        # up to 18:59:50
    assert d.store.get(jid)["held_reason"] == "live" and plat.signals == []
    run_ticks(d, clock, 20)                                          # the window opens
    assert [e["body"]["stop"] for e in events(d, "reservation_start")] == [[jid]]
    sigs = [s for s in plat.signals if s[1] == SIGTERM]
    assert sigs and d.store.get(jid)["stop_reason"] in ("reservation", None)
    run_ticks(d, clock, 30)
    r = d.store.get(jid)
    assert r["state"] == "queued" and r["attempt"] == 2             # requeued by the stop, not failed
    v = client(d, "get", job=jid)
    assert v["blocked_by"] == ["reservation_window:t233-pass"]       # and the window keeps it out until 23:30
    # once per day: the next ticks of the same window do not start anything again
    n = len(plat.signals)
    run_ticks(d, clock, 600)
    assert len(events(d, "reservation_start")) == 1 and len(plat.signals) == n
    assert d.store.meta_get("reservation_latch:t233-pass") == "2026-11-09"
    # the window closes; the job runs again
    clock.set_wall(ms("2026-11-09T23:31:00Z"))
    run_ticks(d, clock, 3)
    assert d.store.get(jid)["state"] == "held"
    # next civil day: the edge fires again
    clock.set_wall(ms("2026-11-10T19:00:05Z"))
    run_ticks(d, clock, 5)
    assert len(events(d, "reservation_start")) == 2
    assert d.store.meta_get("reservation_latch:t233-pass") == "2026-11-10"


def test_reservation_blocks_other_project_and_owner_admits_live(rdaemon, clock):
    d = rdaemon
    clock.set_wall(ms("2026-11-09T20:00:00Z"))
    other = client(d, "submit", spec=spec(project="p", resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    run_ticks(d, clock, 3)
    v = client(d, "get", job=other)
    assert v["state"] == "queued" and v["blocked_by"] == ["reservation_window:t233-pass"]   # the machine is idle
    owner = client(d, "submit", spec=spec(project="latent-alpha", resources={"memory_gib": 4, "gpu": "exclusive"}))["job"]
    run_ticks(d, clock, 3)
    assert d.store.get(owner)["state"] == "held"


def test_reservation_blocked_by_text(rdaemon, clock):
    d = rdaemon
    clock.set_wall(ms("2026-11-09T17:30:00Z"))
    jid = client(d, "submit", spec=spec(project="p", resources={"memory_gib": 25, "gpu": "none"},
                                        runtime={"estimate_s": 3 * 3600}))["job"]
    run_ticks(d, clock, 2)
    v = client(d, "get", job=jid)
    assert v["blocked_by"] == ["reservation_window:t233-pass"]
    assert v["why"] == ("the t233-pass window (19:00\u201323:30) reserves 27 GiB and the GPU for latent-alpha; "
                        "this job would overlap it (ends ~20:30)")
    # a reservation without a GPU claim leaves that clause out
    d.cfg = d.cfg.replace(reservations=(dict(R, gpu="none"),))
    v2 = client(d, "get", job=jid)
    assert v2["why"].startswith("the t233-pass window (19:00\u201323:30) reserves 27 GiB for latent-alpha;")


def test_status_lists_reservations(rdaemon, clock):
    d = rdaemon
    st = client(d, "status")
    r = st["reservations"][0]
    assert r["name"] == "t233-pass" and r["active"] is False and r["project"] == "latent-alpha"
    assert r["next_start_ms"] == ms("2026-11-09T19:00:00Z") and r["next_end_ms"] == ms("2026-11-09T23:30:00Z")
    assert r["reserve_remaining_gib"] == 27.0 and r["owner_jobs"] == []
    assert "windows" not in r                                         # only the dashboard door gets the shading data
    clock.set_wall(ms("2026-11-09T20:00:00Z"))
    own = client(d, "submit", spec=spec(project="latent-alpha", resources={"memory_gib": 10, "gpu": "none"}))["job"]
    run_ticks(d, clock, 3)
    r = client(d, "status")["reservations"][0]
    assert r["active"] is True and r["owner_jobs"] == [own] and r["reserve_remaining_gib"] == 17.0
    assert r["next_end_ms"] == ms("2026-11-09T23:30:00Z")             # while active: the bounds of the current window
    brief = client(d, "status", brief=True)
    assert brief["reservations"][0]["active"] is True
    assert "reservation t233-pass active until 23:30" in gstatus.brief_line(brief)
    dash = client(d, "status", door="dashboard")["reservations"][0]
    assert any(w["start_ms"] == ms("2026-11-09T19:00:00Z") for w in dash["windows"])


def test_status_without_reservations_is_empty(daemon):
    assert client(daemon, "status")["reservations"] == []
