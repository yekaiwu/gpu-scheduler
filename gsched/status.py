"""Job views, `blocked_by` explanations and the `status` document (12 sections 3-5)."""

from __future__ import annotations

import datetime as dt
import json
import os

from gsched.config import Config
from gsched.policy import admit, reservations, windows
from gsched.policy.fit import fits, ledger, live_free_bytes
from gsched.policy.model import Snapshot, running
from gsched.policy.rank import rank_key
from gsched.units import GiB, bytes_to_gib

WARNING_KINDS = ["protective_enter", "spill", "blocking_day", "estimate_overrun", "over_limit",
                 "footprint_unreadable", "abort_tripped", "lost", "tick_slow", "pressure_critical_no_action",
                 "health_lost", "config_reload_failed", "tick_error", "tick_error_persistent", "clock_skew", "job_log_truncated",
                 "reservation_blocked_by_atomic", "cwd_blocked", "yield_app_quit_failed", "yield_app_reopen_failed",
                 "yield_app_busy"]


def iso(ms: int | None, cfg: Config | None = None) -> str | None:
    if ms is None:
        return None
    tz = windows.tz(cfg) if cfg is not None else None
    return dt.datetime.fromtimestamp(ms / 1000, tz=tz).astimezone(tz).isoformat(timespec="seconds") if tz else \
        dt.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def fmt_dur(s: float) -> str:
    s = max(0, int(s))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _g(b: int | None) -> float | None:
    return None if b is None else round(b / GiB, 2)


# ---- explanations -------------------------------------------------------------------------------------------
def explain_all(snap: Snapshot, cfg: Config, offenders: list | None = None) -> dict:
    """job id -> {"blocked_by": [...], "why": str, "position": int|None} for needs_estimate/queued jobs."""
    out: dict = {}
    led = ledger(snap)
    cands = admit.candidates(snap, cfg)
    pos = {j.id: i + 1 for i, j in enumerate(cands)}
    res, res_why = admit.current_reservation(cands, snap, cfg, led)
    byid = {j.id: j for j in snap.jobs}
    holders_gpu = [j for j in running(snap) if j.gpu != "none"]
    dw = admit.day_waiting_ids(snap, cfg)
    for j in snap.jobs:
        if j.state not in ("queued", "needs_estimate"):
            continue
        if j.state == "needs_estimate":
            codes = ["needs_estimate"]
        else:
            ok, why = admit.eligible(j, snap, cfg, dw)
            if not ok:
                codes = [why]
            else:
                fit_ok, codes = fits(j, snap, cfg, led)
                if fit_ok and res is not None and j.id != res.id and admit.competes(j, res, res_why):
                    codes = ["reservation"]
        out[j.id] = {"blocked_by": codes, "position": pos.get(j.id),
                     "why": _why(codes, j, snap, cfg, led, byid, holders_gpu, res, offenders or [])}
    return out


def _why(codes, j, snap, cfg, led, byid, holders_gpu, res, offenders) -> str:
    if not codes:
        return "eligible; will start on the next tick"
    parts = []
    for c in codes:
        parts.append(_why_one(c, j, snap, cfg, led, byid, holders_gpu, res, offenders))
    return "; ".join(p for p in parts if p)


def _why_one(c, j, snap, cfg, led, byid, holders_gpu, res, offenders) -> str:
    need = round(j.memory_bytes / GiB, 1)
    if c == "needs_estimate":
        return f"atomic/cooperative jobs need runtime.estimate_s; run `gsched estimate {j.id} 3h`"
    if c == "dependency":
        ids = [d for d in j.after if snap.dep_states.get(d) not in ("done",)]
        return f"waiting for {len(ids)} job(s): {', '.join(ids)}"
    if c == "dependency_failed":
        bad = [(d, snap.dep_states.get(d)) for d in j.after if snap.dep_states.get(d) in ("failed", "cancelled", "lost", None)]
        d0, s0 = bad[0] if bad else ("?", "?")
        return f"dependency {d0} ended {s0}; this job will be cancelled"
    if c == "no_start_waiter":
        return f"external job: run `gsched wait {j.id} --until start` to receive the lease"
    if c == "window":
        nw = cfg.night_window
        left = (windows.next_window_start_ms(snap.now_ms, cfg) - snap.now_ms) / 1000
        return (f"night-queue job; the night window opens at {nw['start']} (in {fmt_dur(left)}). "
                f"backfill is {'on' if cfg.backfill else 'off'}")
    if c == "fit_window":
        end = windows.next_window_end_ms(snap.now_ms, cfg)
        left = (end - snap.now_ms) / 1000
        return (f"atomic, estimate {fmt_dur(j.estimate_s or 0)} > {fmt_dur(left)} left before "
                f"{cfg.night_window['end']}. Set allow_spill or use queue=day to run anyway")
    if c == "memory_ledger":
        budget = round(snap.budget_bytes / GiB, 1)
        return (f"needs {need} GiB; {round(led.reserved_bytes / GiB, 1)} of {budget} GiB budget is reserved by "
                f"{len(led.holders)} running job(s)")
    if c == "memory_live":
        free = round(max(0, live_free_bytes(snap, cfg, led)) / GiB, 1)
        return (f"needs {need} GiB + {cfg.headroom_gib:g} headroom; only {free} GiB is free right now "
                "(other apps use memory)")
    if c == "gpu":
        h = holders_gpu[0] if holders_gpu else None
        return f"GPU is taken by {h.id} ({h.gpu})" if h else "GPU is taken"
    if c == "cpu":
        return f"needs {j.cpu_cores} cores; {led.cpu_used}/{snap.cpu_budget} in use"
    if c.startswith("lock:"):
        name = c[5:]
        holder = next((r.id for r in running(snap) if name in r.locks), "?")
        return f"lock {name} is held by {holder}"
    if c == "protective":
        o = offenders[0] if offenders else None
        what = f"{o.path} pid {o.pid}" if o and o.path else (f"port {o.port}" if o and o.port else "unknown")
        return f"a foreign GPU-heavy process is running ({what}); GPU jobs wait until it exits"
    if c == "pressure":
        return f"memory pressure is {snap.pressure}; admissions are paused"
    if c == "swap_unknown":
        return "swap usage is unreadable; admissions are paused (fail-closed)"
    if c == "level":
        return f"needs memorystatus level >= {j.level_min}; now {snap.mem.level}"
    if c == "reservation":
        return (f"job {res.id} has waited {fmt_dur((snap.now_ms - (res.eligible_since_ms or snap.now_ms)) / 1000)} "
                "and holds the reservation; this job would delay it") if res else "a reservation is held"
    if c.startswith("reservation_window:"):
        return _why_reservation(c.split(":", 1)[1], j, snap, cfg)
    if c == "inhibited":
        return "this profile is inhibited"
    if c == "drain":
        return "the daemon is draining"
    if c == "project_missing":
        return "the project file is missing"
    return c


def _hhmm_local(ms: int, cfg: Config) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=windows.tz(cfg)).strftime("%H:%M")


def _why_reservation(name: str, j, snap: Snapshot, cfg: Config) -> str:
    r = next((x for x in reservations.parse(cfg) if x.name == name), None)
    if r is None:
        return f"the {name} reservation window"
    est = j.estimate_s if j.estimate_s is not None else cfg.default_unknown_estimate_s
    ends = _hhmm_local(snap.now_ms + est * 1000, cfg)
    what = {"exclusive": " and the GPU", "shared": " and a shared GPU slot", "none": ""}[r.gpu]
    return (f"the {r.name} window ({r.start}\u2013{r.end}) reserves {round(r.memory_bytes / GiB, 1):g} GiB{what} for "
            f"{r.project}; this job would overlap it (ends ~{ends})")


def reservation_status(d, snap: Snapshot, cfg: Config, *, windows_ms: tuple | None = None) -> list:
    """The `reservations` array of `status` (16 section 2). next_start/next_end are the bounds of the window that is
    active now, else of the next one. `windows_ms` (dashboard door) adds every window overlapping [from, to]."""
    out = []
    now = snap.now_ms
    for r in reservations.parse(cfg):
        cur = reservations.current(r, now, cfg)
        nxt = reservations.next_start(r, now, cfg)
        win = cur or nxt
        e = {"name": r.name, "project": r.project, "days": list(r.days), "start": r.start, "end": r.end,
             "memory_gib": round(r.memory_bytes / GiB, 2), "gpu": r.gpu, "unload_restartable": r.unload_restartable,
             "active": cur is not None,
             "next_start": iso(win.start_ms, cfg) if win else None, "next_end": iso(win.end_ms, cfg) if win else None,
             "next_start_ms": win.start_ms if win else None, "next_end_ms": win.end_ms if win else None,
             "reserve_remaining_gib": round(reservations.reserve_remaining(r, snap) / GiB, 2),
             "owner_jobs": [j.id for j in running(snap) if j.project == r.project]}
        if windows_ms is not None:
            lo, hi = windows_ms
            e["windows"] = [{"start_ms": o.start_ms, "end_ms": o.end_ms}
                            for o in reservations.occurrences(r, now, cfg, back_days=2, forward_days=3)
                            if o.end_ms > lo and o.start_ms < hi]
        out.append(e)
    return out


# ---- job views ------------------------------------------------------------------------------------------------------
def job_view(row, cfg: Config, expl: dict | None = None, *, full: bool = True, paths=None,
             daemon_state: dict | None = None) -> dict:
    jid = row["id"]
    e = (expl or {}).get(jid, {})
    v = {
        "job": jid, "project": row["project"], "name": row["name"], "profile": row["profile"],
        "state": row["state"], "held_reason": row["held_reason"], "queue": row["queue"],
        "priority": row["priority"], "checkpoint": row["checkpoint"], "spawn": row["spawn"],
        "resources": {"memory_gib": round(row["memory_bytes"] / GiB, 3), "gpu": row["gpu"],
                      "cpu_cores": row["cpu_cores"], "locks": json.loads(row["locks_json"])},
        "runtime": {"estimate_s": row["estimate_s"], "limit_s": row["limit_s"]},
        "attempt": row["attempt"], "resume": bool(row["resume"]),
        "submitted": iso(row["submitted_ms"], cfg), "enqueued": iso(row["enqueued_ms"], cfg),
        "held": iso(row["held_ms"], cfg), "started": iso(row["started_ms"], cfg),
        "released": iso(row["released_ms"], cfg),
        "submitted_ms": row["submitted_ms"], "enqueued_ms": row["enqueued_ms"], "held_ms": row["held_ms"],
        "started_ms": row["started_ms"], "released_ms": row["released_ms"],
        "blocked_by": e.get("blocked_by", []), "why": e.get("why", ""), "position": e.get("position"),
        "footprint_gib": _g(row["footprint_bytes"]), "peak_footprint_gib": _g(row["peak_footprint_bytes"]),
        "flags": {"spilled": bool(row["spilled"]), "checkpoint_failed": bool(row["checkpoint_failed"]),
                  "over_limit": bool(row["over_limit"]), "cancel_requested": bool(row["cancel_requested"]),
                  "yield_requested": bool(row["yield_requested"]), "estimate_overrun": bool(row["estimate_overrun"]),
                  "blocking_day": bool(row["spilled"]) and not windows.in_night(daemon_state["now_ms"], cfg)
                  if daemon_state and cfg.night_window else False},
        "exit": {"code": row["exit_code"], "signal": row["exit_signal"], "end_reason": row["end_reason"]},
        "stop_reason": row["stop_reason"], "spill_reason": row["spill_reason"], "ready": bool(row["ready"]),
        "pid": row["pid"], "after": json.loads(row["after_json"]), "warnings": [],
        "door": row["door"], "comment": row["comment"],
    }
    if row["state"] == "held" and not e.get("why"):
        v["why"] = f"running ({row['held_reason']})"
    if full:
        v["command"] = json.loads(row["command_json"]) if row["command_json"] else None
        v["cwd"] = row["cwd"]
        v["env_keys"] = sorted(json.loads(row["env_json"]))
        if paths is not None:
            jd = paths.job_dir(jid)
            v["logs"] = {"stdout": row["stdout_path"] or f"{jd}/stdout.log",
                         "stderr": row["stderr_path"] or f"{jd}/stderr.log"}
        v["checkpoint_dir"] = row["checkpoint_dir"]
    return v


# ---- the status document ------------------------------------------------------------------------------------------
def _brief_view(row, e, cfg, d) -> dict:
    now = d.clock.now_ms()
    v = {"job": row["id"], "project": row["project"], "name": row["name"], "state": row["state"],
         "held_reason": row["held_reason"], "queue": row["queue"], "priority": row["priority"],
         "checkpoint": row["checkpoint"], "memory_gib": round(row["memory_bytes"] / GiB, 2), "gpu": row["gpu"],
         "cpu_cores": row["cpu_cores"], "estimate_s": row["estimate_s"], "attempt": row["attempt"],
         "submitted_ms": row["submitted_ms"], "enqueued_ms": row["enqueued_ms"], "started_ms": row["started_ms"],
         "profile": row["profile"], "spawn": row["spawn"]}
    if row["state"] == "held":
        fp = d.last_scan.job_footprint.get(row["id"]) if d.last_scan else None
        v["footprint_gib"] = _g(fp if fp is not None else row["footprint_bytes"])
        v["peak_footprint_gib"] = _g(row["peak_footprint_bytes"])
        v["runtime_s"] = round((now - row["started_ms"]) / 1000) if row["started_ms"] else None
        v["flags"] = {"spilled": bool(row["spilled"]), "over_limit": bool(row["over_limit"]),
                      "cancel_requested": bool(row["cancel_requested"]),
                      "estimate_overrun": bool(row["estimate_overrun"]), "yield_requested": bool(row["yield_requested"])}
    else:
        v["blocked_by"] = e.get("blocked_by", [])
        v["why"] = e.get("why", "")
        v["position"] = e.get("position")
        v["waiting_s"] = round((now - row["enqueued_ms"]) / 1000)
    return v


def build_status(d, brief: bool = False, dashboard: bool = False) -> dict:
    cfg = d.cfg
    snap = d.snapshot()
    now = snap.now_ms
    led = ledger(snap)
    m = d.machine
    s = snap.mem
    pg = s.page_size
    live_free = live_free_bytes(snap, cfg, led)
    ex_job = next((j.id for j in running(snap) if j.gpu == "exclusive"), None)
    sh_jobs = [j.id for j in running(snap) if j.gpu == "shared"]
    g = d.platform.gpu_stats()
    expl = explain_all(snap, cfg, list(d.protective.reasons))
    rows = d.store.live_rows()
    run_rows = [r for r in rows if r["state"] == "held"]
    q_rows = [r for r in rows if r["state"] in ("queued", "needs_estimate")]
    q_rows.sort(key=lambda r: (expl.get(r["id"], {}).get("position") or 10**9, r["enqueued_ms"], r["id"]))
    memory = {
        "budget_bytes": d.budget_bytes, "budget_gib": round(d.budget_bytes / GiB, 2),
        "reserved_gib": round(led.reserved_bytes / GiB, 2), "pending_gib": round(led.pending_bytes / GiB, 2),
        "reclaimable_gib": round(s.reclaimable_bytes / GiB, 2), "live_free_gib": round(live_free / GiB, 2),
        "reclaimable_parts_gib": {"free": round(s.free_pages * pg / GiB, 2),
                                  "purgeable": round(s.purgeable_pages * pg / GiB, 2),
                                  "file_backed": round(s.file_backed_pages * pg / GiB, 2)},
        "level": s.level, "swap_used_gib": None if s.swap_used_bytes is None else round(s.swap_used_bytes / GiB, 2),
        "swap_total_gib": None if s.swap_total_bytes is None else round(s.swap_total_bytes / GiB, 2),
        "swap_known": s.swap_used_bytes is not None,
        "swap_rise_gib_per_min": None if d.swap_rate is None else round(d.swap_rate, 3),
        "headroom_gib": cfg.headroom_gib,
    }
    queue_counts: dict = {}
    for jid, e in expl.items():
        for c in (e["blocked_by"] or ["eligible"]):
            queue_counts[c] = queue_counts.get(c, 0) + 1
    out = {
        "version": __import__("gsched").__version__, "daemon_pid": os.getpid(),
        "uptime_s": round(d.clock.mono() - d.start_mono, 1), "enforce": cfg.enforce, "draining": d.draining,
        "machine": {"chip": m.chip, "memsize_bytes": m.memsize_bytes, "memsize_gib": round(m.memsize_bytes / GiB, 2),
                    "physical_cpu": m.physical_cpu, "perf_cores": m.perf_cores, "eff_cores": m.eff_cores,
                    "os": m.os_version, "gpu_wired_limit_mb": m.gpu_wired_limit_mb},
        "memory": memory,
        "gpu": {"exclusive_job": ex_job, "shared_jobs": sh_jobs, "shared_max": cfg.gpu_shared_max,
                "device_util_pct": g.device_util_pct,
                "in_use_gib": None if g.in_use_bytes is None else round(g.in_use_bytes / GiB, 2)},
        "cpu": {"budget": d.cpu_budget, "used": led.cpu_used},
        "pressure": d.pressure, "pressure_why": d.pressure_why, "protective": d.protective.active,
        "sleep_prevented": d.supervisor.sleep_prevented(),
        "reservation": d.reservation,
        "now_ms": now,
    }
    out["yield_apps"] = d.yield_apps.status() if cfg.yield_apps else []
    out["reservations"] = reservation_status(d, snap, cfg, windows_ms=(now - 36 * 3600_000, now + 36 * 3600_000)
                                             if dashboard else None)
    if cfg.night_window is not None:
        out["night_window"] = {"start": cfg.night_window["start"], "end": cfg.night_window["end"],
                               "tz": str(windows.tz(cfg)), "in_night": windows.in_night(now, cfg),
                               "next_end": iso(windows.next_window_end_ms(now, cfg), cfg),
                               "next_end_ms": windows.next_window_end_ms(now, cfg),
                               "next_start_ms": windows.next_window_start_ms(now, cfg)}
    else:
        out["night_window"] = None
    out["running"] = [_brief_view(r, expl.get(r["id"], {}), cfg, d) for r in run_rows]
    if brief:
        out["queue_counts"] = queue_counts
        out["queued"] = len(q_rows)
        out["queue"] = []
        return out
    out["queue"] = [_brief_view(r, expl.get(r["id"], {}), cfg, d) for r in q_rows]
    out["queue_counts"] = queue_counts
    out["queued"] = len(q_rows)
    out["projects"] = [{"id": p["id"], "weight": p["weight"], "usage": round(p["usage"], 1),
                        "usage_per_weight": round(p["usage"] / p["weight"], 1), "present": bool(p["present"])}
                       for p in d.store.projects()]
    out["offenders"] = [{"pid": o["pid"], "path": o["path"], "port": o["port"], "class": o["class"],
                         "footprint_gib": _g(o["footprint_bytes"]), "since": iso(o["seen_ms"], cfg)}
                        for o in d.store.offender_open()]
    seen, warns = set(), []
    for e in d.store.events(kinds=WARNING_KINDS, since_ms=now - 86_400_000, limit=500, newest_first=True):
        if e["kind"] not in seen:
            seen.add(e["kind"])
            warns.append({"kind": e["kind"], "job": e["job"], "t": iso(e["t_ms"], cfg), "t_ms": e["t_ms"]})
    out["warnings"] = warns
    out["profiles"] = d.residents.status_list()
    return out


def brief_line(st: dict) -> str:
    m, mem = st["machine"], st["memory"]
    gpu = st["gpu"]
    g = "exclusive(1)" if gpu["exclusive_job"] else (f"shared({len(gpu['shared_jobs'])})" if gpu["shared_jobs"] else "idle")
    res = [r for r in st.get("reservations", []) if r["active"]]
    tail = "".join(f" · reservation {r['name']} active until {(r['next_end'] or '')[11:16]}" for r in res)
    tail += "".join(f" · {a['name']} closed for {a['reason']}" for a in st.get("yield_apps", []) if a["phase"] == "closed")
    return (f"{m['memsize_gib']:.1f} GiB machine · budget {mem['budget_gib']:.1f} · reserved {mem['reserved_gib']:.1f} · "
            f"live free {mem['live_free_gib']:.1f} · GPU {g} · pressure {st['pressure']} · {len(st['running'])} running · "
            f"{st['queued']} queued · enforce {'on' if st['enforce'] else 'off'}{tail}")
