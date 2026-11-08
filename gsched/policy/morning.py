"""Morning edge (06 section 7). Pure."""

from __future__ import annotations

from gsched.config import Config
from gsched.policy import admit, preempt, windows
from gsched.policy.fit import fits, ledger
from gsched.policy.model import Plan, Snapshot, running


def top_day_waiter(snap: Snapshot, cfg: Config):
    led = ledger(snap)
    for j in admit.candidates(snap, cfg):
        if j.queue == "day" and not fits(j, snap, cfg, led)[0]:
            return j
    return None


def plan(snap: Snapshot, cfg: Config) -> Plan:
    if cfg.night_window is None or not cfg.enforce:
        return Plan.empty()
    today = windows.civil_date(snap.now_ms, cfg)
    if snap.morning_latch == today or windows.in_night(snap.now_ms, cfg):
        return Plan.empty()
    # "the first tick at or after night_window.end" on this civil date. For a window that wraps midnight
    # (23:00-08:00) not-in-night already implies that; for one that does not (01:00-06:00) the hours between
    # midnight and the start are outside the window but before today's end, and must not take the edge.
    if not windows.past_end_today(snap.now_ms, cfg):
        return Plan.empty()
    p = Plan(set_meta={"morning_latch": today})
    for j in running(snap):
        if j.spawn == "daemon" and j.checkpoint == "cooperative" and j.queue == "night" and j.held_reason == "live" \
                and not j.spilled and not j.checkpoint_failed:
            p.checkpoint.append((j.id, "morning"))
    if cfg.morning_unload_restartable:
        target = top_day_waiter(snap, cfg)
        if target is not None:
            victims = preempt.find_victims(target, snap, cfg, mode="morning")
            p.stop += [(v, "morning") for v in victims]
            if victims:
                p.reserve = target.id
                p.freeze_admission = True
    p.events.append(("morning_edge", None, {"date": today, "checkpoint": [c[0] for c in p.checkpoint],
                                            "stop": [s[0] for s in p.stop]}))
    return p
