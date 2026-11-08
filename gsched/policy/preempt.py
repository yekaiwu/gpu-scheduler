"""Victim planner (06 section 8). Pure. Only restartable/cooperative jobs the daemon spawned can be victims."""

from __future__ import annotations

from typing import Literal

from gsched.config import Config
from gsched.policy import admit
from gsched.policy.fit import fits, ledger
from gsched.policy.model import Plan, Snapshot, running

Mode = Literal["priority", "reservation", "morning", "pressure", "window"]


def _candidates(target, snap: Snapshot, cfg: Config, mode: Mode):
    cands = []
    for v in running(snap):
        if v.spawn != "daemon" or v.held_reason != "live":
            continue
        if v.checkpoint == "atomic" or v.spilled:
            continue
        held_s = (snap.now_ms - (v.started_ms or snap.now_ms)) / 1000
        if mode == "window":
            # time-reservation start (16 section 2): jobs of OTHER projects, priority and preempt_restartable ignored,
            # minimum holds ignored (the window opens now); resident `hold` is still honoured
            if v.project == target.project or v.checkpoint not in ("restartable", "cooperative"):
                continue
            if v.checkpoint == "restartable" and v.hold:
                continue
            cands.append(v)
            continue
        if v.checkpoint == "restartable":
            if mode in ("priority", "reservation") and not cfg.preempt_restartable:
                continue
            if held_s < cfg.restartable_min_hold_s and mode != "pressure":
                continue
            if mode == "priority" and not v.priority < target.priority:
                continue
            if mode == "reservation" and not v.priority <= target.priority:
                continue
            if v.hold and mode != "pressure":
                continue
        elif v.checkpoint == "cooperative":
            if mode == "morning":
                continue
            if mode == "pressure" and not cfg.pressure_checkpoint_cooperative:
                continue
            if mode != "pressure" and not v.priority < target.priority:
                continue
            if held_s < cfg.coop_min_hold_s and mode != "pressure":
                continue
        else:
            continue
        cands.append(v)
    # restartable before cooperative, lowest priority, highest project usage/weight, newest started
    cands.sort(key=lambda v: (v.checkpoint != "restartable", v.priority,
                              -v.project_usage / max(v.project_weight, 1), -(v.started_ms or 0)))
    return cands


def find_victims(target, snap: Snapshot, cfg: Config, mode: Mode) -> list[str]:
    cands = _candidates(target, snap, cfg, mode)
    chosen: list[str] = []
    for v in cands:
        chosen.append(v.id)
        if fits(target, snap, cfg, ignoring=frozenset(chosen))[0]:
            return _minimize(chosen, target, snap, cfg)
    return []   # cannot make it fit -> signal nobody


def _minimize(chosen: list[str], target, snap: Snapshot, cfg: Config) -> list[str]:
    keep = list(chosen)
    for v in reversed(chosen):
        trial = [x for x in keep if x != v]
        if trial and fits(target, snap, cfg, ignoring=frozenset(trial))[0]:
            keep = trial
    return keep


def plan(snap: Snapshot, cfg: Config) -> Plan:
    if not cfg.enforce:
        return Plan.empty()
    if any(j.held_reason in ("stopping", "checkpointing") and j.stop_reason in ("preempt", "reservation")
           for j in running(snap)):
        return Plan(freeze_admission=True)       # a preemption is already in flight
    cands = admit.candidates(snap, cfg)
    led = ledger(snap)
    res, _ = admit.current_reservation(cands, snap, cfg, led)
    by = {j.id: j for j in running(snap)}
    for t in cands:
        ok, _why = fits(t, snap, cfg, led)
        if ok:
            continue
        mode = "reservation" if (res is not None and res.id == t.id) else "priority"
        victims = find_victims(t, snap, cfg, mode)
        if not victims:
            continue
        p = Plan(reserve=t.id, freeze_admission=True)
        for vid in victims:
            if by[vid].checkpoint == "restartable":
                p.stop.append((vid, "preempt"))
            else:
                p.checkpoint.append((vid, "preempt"))
        p.events.append(("preempt_plan", t.id, {"target": t.id, "victims": victims, "mode": mode}))
        return p
    return Plan.empty()
