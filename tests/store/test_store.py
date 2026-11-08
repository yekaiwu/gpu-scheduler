import json
import os
import random
import sqlite3
import stat

import pytest

from gsched.errors import StoreError
from gsched.paths import Paths
from gsched.platform import JobIdentity
from gsched.store import Store
from tests.helpers import make_spec

IDENT = JobIdentity(100, 1, 2, "/bin/x", 100)


def add(store, now=1000, **over):
    spec = make_spec(**over)
    queue, state = ("day", "queued")
    return store.insert_job(spec, queue=queue, state=state, door="cli", now_ms=now)


def test_user_version_mismatch_exits_78(paths, clock):
    Store(paths, clock).close()
    c = sqlite3.connect(paths.db)
    c.execute("PRAGMA user_version=7")
    c.close()
    with pytest.raises(StoreError) as e:
        Store(paths, clock)
    assert "schema version 7" in str(e.value)


def test_db_file_mode_0600(store):
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    j = add(store)
    assert stat.S_IMODE(os.stat(store.paths.job_dir(j)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.paths.checkpoint_dir(j)).st_mode) == 0o700
    spec = json.load(open(os.path.join(store.paths.job_dir(j), "spec.json")))
    assert "env_keys" in spec and "env" not in spec


def test_env_values_not_in_spec_json(store):
    j = add(store, env={"SECRET": "hunter2"})
    txt = open(os.path.join(store.paths.job_dir(j), "spec.json")).read()
    assert "hunter2" not in txt and "SECRET" in txt


def test_transition_wrong_state_returns_false(store):
    j = add(store)
    assert not store.mark_gate_released(j, 1)
    assert not store.request_checkpoint(j, 1, "x")
    assert not store.begin_stop(j, 1, "preempt")
    assert not store.mark_lost(j, 1, "x")
    assert store.finish(j, 1, exit_code=0) is None
    assert store.commit_admit(j, 2)
    assert not store.commit_admit(j, 3)       # already held
    assert not store.cancel_unstarted(j, 3, "x")


def test_commit_before_return(store, paths, clock):
    j = add(store)
    store.commit_admit(j, 5)
    other = sqlite3.connect(paths.db)
    assert other.execute("SELECT state FROM job WHERE id=?", (j,)).fetchone()[0] == "held"
    other.close()


def test_idempotency_index_is_unique(store):
    add(store, idempotency_key="k")
    with pytest.raises(sqlite3.IntegrityError):
        add(store, idempotency_key="k")
    add(store, idempotency_key="k", project="q")   # different project is fine
    assert store.find_idempotent("p", "k") is not None


def test_idempotency_released_when_terminal(store):
    j = add(store, idempotency_key="k")
    assert store.cancel_unstarted(j, 5, "cancelled")
    add(store, idempotency_key="k")


def test_profile_live_unique(store):
    spec = make_spec()
    store.insert_job(spec, queue="day", state="queued", door="profile", now_ms=1, profile="llama")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_job(spec, queue="day", state="queued", door="profile", now_ms=2, profile="llama")


def test_retention_deletes_old_events(store):
    now = 100 * 86400 * 1000
    store.add_event(now - 40 * 86400 * 1000, "old", None, {})
    store.add_event(now - 1000, "new", None, {})
    j = add(store)
    store.commit_admit(j, 1)
    store.finish(j, 1000, exit_code=0)
    r = store.retention(now, 30)
    assert r["events"] == 1 and r["job_dirs"] == 1
    assert [e["kind"] for e in store.events(since_ms=0)] == ["new"]
    assert not os.path.exists(store.paths.job_dir(j))


def lifecycle_to_live(store, j, now=10):
    assert store.commit_admit(j, now)
    assert store.record_shim(j, 99, 1, 1)
    assert store.begin_child(j, IDENT, now)
    r = store.get(j)
    assert r["held_reason"] == "spawning" and r["gate_released"] == 0 and r["pid"] == 100
    assert store.mark_gate_released(j, now + 1)
    r = store.get(j)
    assert r["held_reason"] == "live" and r["started_ms"] == now + 1


def test_daemon_lifecycle_finish_mapping(store):
    for code, sig, cancel, state, reason in [(0, None, False, "done", "exit"), (3, None, False, "failed", "exit"),
                                              (None, 9, False, "failed", "signal"), (0, None, True, "cancelled", "cancelled")]:
        j = add(store)
        lifecycle_to_live(store, j)
        if cancel:
            assert store.mark_cancel_requested(j)
        assert store.finish(j, 50, exit_code=code, exit_signal=sig, peak_footprint_bytes=7) == state
        r = store.get(j)
        assert r["end_reason"] == reason and r["released_ms"] == 50 and r["held_reason"] is None
    assert store.conn.execute("select count(*) from history").fetchone()[0] == 4


def test_cooperative_requeue_same_id(store):
    j = add(store, checkpoint="cooperative")
    lifecycle_to_live(store, j)
    assert store.request_checkpoint(j, 100, "morning")
    assert store.get(j)["held_reason"] == "checkpointing"
    assert not store.request_checkpoint(j, 101, "morning")      # one request per attempt
    assert store.requeue_continuation(j, 200, "night")
    r = store.get(j)
    assert r["state"] == "queued" and r["attempt"] == 2 and r["resume"] == 1 and r["enqueued_ms"] == 200
    assert r["pid"] is None and r["shim_pid"] is None and r["queue"] == "night" and r["submitted_ms"] == 1000


def test_spill_and_terminate_rules(store):
    j = add(store, checkpoint="cooperative")
    lifecycle_to_live(store, j)
    store.request_checkpoint(j, 100, "x")
    assert not store.begin_stop(j, 101, "preempt")             # not allowed from checkpointing
    assert store.mark_spilled(j, "checkpoint_timeout")
    r = store.get(j)
    assert r["spilled"] == 1 and r["held_reason"] == "spilled"
    assert not store.begin_stop(j, 102, "preempt")
    assert store.begin_stop(j, 103, "terminate")
    assert store.get(j)["held_reason"] == "stopping"


def test_requeue_unstarted_and_restartable(store):
    j = add(store, checkpoint="restartable")
    store.commit_admit(j, 1)
    assert store.requeue_unstarted(j, 2)
    r = store.get(j)
    assert r["state"] == "queued" and r["attempt"] == 1
    lifecycle_to_live(store, j)
    assert not store.requeue_unstarted(j, 3)                     # gate released
    assert store.begin_stop(j, 20, "preempt")
    assert store.requeue_restartable(j, 30)
    r = store.get(j)
    assert r["state"] == "queued" and r["attempt"] == 2 and r["resume"] == 0
    lifecycle_to_live(store, j, now=40)
    store.begin_stop(j, 50, "idle")
    assert store.requeue_restartable(j, 60, to_state="done", exit_code=143, end_reason="idle_unload")
    assert store.get(j)["state"] == "done" and store.get(j)["end_reason"] == "idle_unload"


def test_set_flags_whitelist(store):
    j = add(store)
    assert store.set_flags(j, hold=1, last_touch_ms=5)
    with pytest.raises(ValueError):
        store.set_flags(j, state="done")
    assert store.get(j)["state"] == "queued"


def test_external_flow(store):
    j = add(store, spawn="external", command=None)
    assert store.commit_admit(j, 1, lease_hash="h")
    r = store.get(j)
    assert r["held_reason"] == "await_attach" and r["lease_hash"] == "h"
    assert store.begin_child(j, IDENT, 2)
    r = store.get(j)
    assert r["held_reason"] == "live" and r["gate_released"] == 1 and r["started_ms"] == 2
    assert not store.begin_child(j, IDENT, 3)


def test_events_filters(store):
    for i in range(5):
        store.add_event(i * 1000, "sample" if i % 2 else "admit", "j1" if i < 3 else None, {"i": i})
    assert len(store.events(kinds=["sample"])) == 2
    assert len(store.events(exclude_kinds=["sample"])) == 3
    assert [e["body"]["i"] for e in store.events(limit=2)] == [3, 4]
    assert store.events(since_ms=3000)[0]["body"]["i"] == 3
    assert len(store.events(job="j1")) == 3


def test_invariants_hold_after_random_ops(store):
    rnd = random.Random(7)
    jobs: list[str] = []
    for step in range(500):
        op = rnd.choice(["add", "admit", "live", "finish", "cancel", "stop", "ckpt"])
        if op == "add" or not jobs:
            gpu = rnd.choice(["none", "shared", "exclusive"])
            jobs.append(add(store, now=1000 + step, resources={"memory_gib": 1, "gpu": gpu},
                            checkpoint=rnd.choice(["atomic", "cooperative", "restartable"])))
            continue
        j = rnd.choice(jobs)
        r = store.get(j)
        if op == "admit" and r["state"] == "queued":
            # honour the admission rules the policy would enforce
            held = store.held_jobs()
            if r["gpu"] == "exclusive" and any(h["gpu"] != "none" for h in held):
                continue
            if r["gpu"] == "shared" and any(h["gpu"] == "exclusive" for h in held):
                continue
            if r["gpu"] == "shared" and sum(1 for h in held if h["gpu"] == "shared") >= 2:
                continue
            store.commit_admit(j, step)
        elif op == "live" and r["state"] == "held" and r["held_reason"] == "spawning":
            store.record_shim(j, 1000 + step, 1, 1)
            store.begin_child(j, JobIdentity(2000 + step, 1, 1, "/x", 1000 + step), step)
            store.mark_gate_released(j, step)
        elif op == "finish" and r["state"] == "held":
            store.finish(j, step, exit_code=0)
        elif op == "cancel":
            store.cancel_unstarted(j, step, "cancelled")
        elif op == "stop" and r["state"] == "held":
            store.begin_stop(j, step, "preempt")
        elif op == "ckpt" and r["state"] == "held":
            store.request_checkpoint(j, step, "morning")
        assert store.check_invariants(budget_bytes=10**12, gpu_shared_max=2) == []


def test_batch_single_transaction(store, paths):
    j = add(store)
    other = sqlite3.connect(paths.db)
    with store.batch():
        store.set_flags(j, hold=1)
        assert other.execute("SELECT hold FROM job WHERE id=?", (j,)).fetchone()[0] == 0
    assert other.execute("SELECT hold FROM job WHERE id=?", (j,)).fetchone()[0] == 1
    other.close()
