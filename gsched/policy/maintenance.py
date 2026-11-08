"""Per-job maintenance (06 section 11). Pure.

Stopping rules end evaluation for a job on that tick; flag-only rules (over_limit, estimate_overrun,
ceiling notice) do not. Nothing here ever targets an atomic or spilled job with a signal.
"""

from __future__ import annotations

from gsched.config import Config
from gsched.policy.model import Plan, Snapshot, running
from gsched.units import GiB


def _abort_trip(j, snap: Snapshot) -> dict | None:
    a = j.abort
    base = snap.baselines.get(j.id)
    if a is None or not a.armed or not j.ready or base is None:
        return None
    m = snap.mem
    if a.level_below is not None and m.level is not None and m.level < a.level_below:
        return {"line": "abort_level_below", "level": m.level}
    if a.reclaimable_below_gib is not None and m.reclaimable_bytes < a.reclaimable_below_gib * GiB:
        return {"line": "abort_reclaimable_below_gib", "reclaimable_gib": round(m.reclaimable_bytes / GiB, 2)}
    if a.swap_rise_gib is not None and m.swap_used_bytes is not None and base.swap_used_bytes is not None \
            and (m.swap_used_bytes - base.swap_used_bytes) > a.swap_rise_gib * GiB:
        return {"line": "abort_swap_rise_gib", "rise_gib": round((m.swap_used_bytes - base.swap_used_bytes) / GiB, 2)}
    if a.ceiling_gib is not None and j.footprint_bytes is not None and j.footprint_bytes > a.ceiling_gib * GiB:
        return {"line": "abort_ceiling_gib", "footprint_gib": round(j.footprint_bytes / GiB, 2)}
    if j.abort_unavail_since_ms is not None and snap.now_ms - j.abort_unavail_since_ms >= 30_000:
        return {"line": "abort_sample_unavailable"}
    return None


def plan(snap: Snapshot, cfg: Config) -> Plan:
    p = Plan()
    now = snap.now_ms
    for j in running(snap):
        ours = j.spawn == "daemon"
        # 1. SIGKILL escalation, once
        if j.held_reason == "stopping":
            if ours and not j.kill_sent and j.stop_requested_ms is not None \
                    and now - j.stop_requested_ms >= cfg.kill_grace_s * 1000:
                p.kill.append(j.id)
            continue
        # 2. checkpoint grace expired
        if j.held_reason == "checkpointing":
            if ours and j.stop_requested_ms is not None and now - j.stop_requested_ms >= j.grace_s * 1000:
                if j.on_timeout == "kill" and j.checkpoint == "cooperative":
                    p.events.append(("work_lost_since_last_ready", j.id, {"reason": "checkpoint_timeout_kill"}))
                    p.kill_stop.append((j.id, "checkpoint_timeout_kill"))
                else:
                    p.spill.append((j.id, "checkpoint_timeout"))
            continue
        if j.held_reason == "spilled":
            # treated as atomic until it exits: flags and events only, never a signal (DESIGN 7, 8)
            started = j.started_ms
            if j.limit_s and started is not None and now - started >= j.limit_s * 1000 and not j.over_limit:
                p.flags.append((j.id, {"over_limit": 1}))
                p.events.append(("over_limit", j.id, {"limit_s": j.limit_s}))
            if j.estimate_s and started is not None and now - started > j.estimate_s * 1000 and not j.estimate_overrun:
                p.flags.append((j.id, {"estimate_overrun": 1}))
                p.events.append(("estimate_overrun", j.id, {"estimate_s": j.estimate_s}))
            continue
        if j.held_reason != "live":
            continue     # spawning, await_attach: nothing on policy
        started = j.started_ms
        # 3. runtime limit, counted from process start
        if j.limit_s and started is not None and now - started >= j.limit_s * 1000:
            if not ours:
                if not j.yield_requested:
                    p.flags.append((j.id, {"yield_requested": 1}))
                    p.events.append(("over_limit", j.id, {"external": True}))
            elif j.checkpoint == "restartable":
                p.stop.append((j.id, "limit"))
                continue
            elif j.checkpoint == "cooperative" and not j.spilled:
                p.checkpoint.append((j.id, "limit"))
                continue
            elif not j.over_limit:      # atomic or spilled: mark only, never signal
                p.flags.append((j.id, {"over_limit": 1}))
                p.events.append(("over_limit", j.id, {"limit_s": j.limit_s}))
        # 4. ceiling
        if j.ceiling_bytes and j.footprint_bytes is not None and j.footprint_bytes > j.ceiling_bytes:
            if not j.ceiling_noted:
                p.events.append(("ceiling_exceeded", j.id, {"footprint_bytes": j.footprint_bytes,
                                                            "ceiling_bytes": j.ceiling_bytes}))
            if ours and j.checkpoint == "restartable":
                p.stop.append((j.id, "ceiling"))
                continue
        # 5. estimate overrun
        if j.estimate_s and started is not None and now - started > j.estimate_s * 1000 and not j.estimate_overrun:
            p.flags.append((j.id, {"estimate_overrun": 1}))
            p.events.append(("estimate_overrun", j.id, {"estimate_s": j.estimate_s}))
        if not (ours and j.checkpoint == "restartable"):
            # 9 (non-restartable): note an unreadable footprint, no signal
            if j.footprint_fail_n >= 5 and j.footprint_fail_n == 5:
                p.events.append(("footprint_unreadable", j.id, {}))
            continue
        # restartable, ours -----------------------------------------------------------------
        if snap.draining and j.profile is not None and not j.hold:
            p.stop.append((j.id, "drain"))
            continue
        if j.profile is not None and not j.hold and j.idle_unload_s and j.ready and j.last_touch_ms is not None \
                and now - j.last_touch_ms >= j.idle_unload_s * 1000:
            p.stop.append((j.id, "idle"))
            continue
        if j.switch_to and started is not None and now - started >= cfg.restartable_min_hold_s * 1000:
            p.stop.append((j.id, "switch"))
            continue
        trip = _abort_trip(j, snap)
        if trip:
            p.events.append(("abort_tripped", j.id, trip))
            p.stop.append((j.id, "abort"))
            continue
        if j.footprint_fail_n >= 5:
            p.events.append(("footprint_unreadable", j.id, {}))
            p.stop.append((j.id, "footprint_unreadable"))
    return p
