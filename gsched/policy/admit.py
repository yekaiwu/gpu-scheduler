"""Eligibility, reservation and admission (06 sections 2 and 6, 9). Pure."""

from __future__ import annotations

from dataclasses import replace

from gsched.config import Config
from gsched.policy import reservations, windows
from gsched.policy.fit import fits, ledger
from gsched.policy.model import Plan, Snapshot
from gsched.policy.rank import rank_key

# reasons that are not resources a running job will free
NON_RESOURCE = {"level", "pressure", "protective", "swap_unknown"}


def deps_state(j, snap: Snapshot) -> str:
    """ok | waiting | failed"""
    waiting = False
    for d in j.after:
        s = snap.dep_states.get(d)
        if s == "done":
            continue
        if s in ("failed", "cancelled", "lost") or s is None:
            return "failed"
        waiting = True
    return "waiting" if waiting else "ok"


def _core(j, snap: Snapshot, cfg: Config) -> str | None:
    """Every eligibility check except the window checks. Returns a blocking reason or None."""
    if j.state != "queued":
        return j.state
    if not j.project_present:
        return "project_missing"
    if j.inhibited:
        return "inhibited"
    if snap.draining and j.profile is not None:
        return "drain"
    dep = deps_state(j, snap)
    if dep == "waiting":
        return "dependency"
    if dep == "failed":
        return "dependency_failed"
    if j.spawn == "external" and not j.start_waiter_parked:
        return "no_start_waiter"
    return None


def day_waiting_ids(snap: Snapshot, cfg: Config) -> frozenset:
    return frozenset(j.id for j in snap.jobs if j.queue == "day" and _core(j, snap, cfg) is None)


def eligible(j, snap: Snapshot, cfg: Config, day_waiting: frozenset | None = None) -> tuple[bool, str | None]:
    r = _core(j, snap, cfg)
    if r is not None:
        return False, r
    dw = day_waiting_ids(snap, cfg) if day_waiting is None else day_waiting
    jq = j
    if cfg.reservations and j.queue == "night" and reservations.owner_window_active(j, snap.now_ms, cfg):
        jq = replace(j, queue="day")        # their own reservation window: the night-queue restriction is lifted
    if not windows.queue_open(jq, snap.now_ms, cfg, day_waiting=bool(dw - {j.id})):
        return False, "window"
    if not windows.atomic_fits(jq, snap.now_ms, cfg):
        return False, "fit_window"
    return True, None


def candidates(snap: Snapshot, cfg: Config) -> list:
    dw = day_waiting_ids(snap, cfg)
    c = [j for j in snap.jobs if eligible(j, snap, cfg, dw)[0]]
    c.sort(key=lambda j: rank_key(j, snap))
    return c


def current_reservation(cands, snap: Snapshot, cfg: Config, led=None):
    """The highest-ranked candidate that has waited >= starve_s without fitting, for resource reasons only.
    Returns (job, reasons) or (None, [])."""
    led = led or ledger(snap)
    for j in cands:
        ok, why = fits(j, snap, cfg, led)
        if ok:
            continue
        if j.eligible_since_ms is None or (snap.now_ms - j.eligible_since_ms) < cfg.starve_s * 1000:
            continue
        if set(why) & NON_RESOURCE or any(c.startswith("reservation_window:") for c in why):
            continue
        return j, why
    return None, []


def competes(j, res, res_reasons: list[str]) -> bool:
    for r in res_reasons:
        if r in ("memory_ledger", "memory_live") and j.memory_bytes > 0:
            return True
        if r == "gpu" and j.gpu != "none":
            return True
        if r == "cpu":
            return True
        if r.startswith("lock:") and r[5:] in j.locks:
            return True
    return False


def plan(snap: Snapshot, cfg: Config) -> Plan:
    if not cfg.enforce:
        return Plan.empty()
    led = ledger(snap)
    cands = candidates(snap, cfg)
    res, res_why = current_reservation(cands, snap, cfg, led)
    blocked = {}
    for j in cands:
        ok, why = fits(j, snap, cfg, led)
        if ok:
            if res is not None and j.id != res.id and competes(j, res, res_why):
                blocked[j.id] = ["reservation"]
                continue
            return Plan(admit=j.id, reserve=res.id if res else None, blocked=blocked)
        blocked[j.id] = why
    return Plan(blocked=blocked, reserve=res.id if res else None)


def explain_new(j, snap: Snapshot, cfg: Config) -> dict:
    """Observe-mode `would` for a hypothetical queued job."""
    if j.state == "needs_estimate":
        return {"would": "needs_estimate"}
    snap2 = replace(snap, jobs=snap.jobs + (j,))
    dw = day_waiting_ids(snap2, cfg)
    ok, why = eligible(j, snap2, cfg, dw)
    if not ok:
        return {"would": "queue", "blocked_by": [why]}
    fit_ok, reasons = fits(j, snap2, cfg, ledger(snap2))
    if fit_ok:
        # a higher-ranked eligible job would be admitted first, but this one fits independently
        return {"would": "admit_now"}
    return {"would": "queue", "blocked_by": reasons}
