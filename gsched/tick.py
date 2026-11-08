"""The tick: the only place scheduling happens (06). Planners (gsched/policy) are pure; this module executes plans."""

from __future__ import annotations

import json
import os
import time
import traceback

from gsched import ids
from gsched.policy import admit, maintenance, morning, pressure, preempt, reservations, windows
from gsched.policy.fit import compute_budget, compute_cpu_budget, ledger
from gsched.policy.model import JobView, Plan, Snapshot, running
from gsched.policy.rank import charge, rank_key
from gsched.policy.fit import fits
from gsched.errors import PlatformError
from gsched.scan import ScanResult
from gsched.units import GiB

SAMPLE_EVERY_MS = 60_000
SKEW_THRESHOLD_MS = 30_000          # wall vs monotonic drift between full ticks that counts as a sleep / clock jump
LOG_CHECK_EVERY_S = 10.0            # per job, how often the log-size cap is evaluated
PERSISTENT_AFTER = 20               # consecutive failing ticks of one phase before tick_error_persistent


def _truncate_keep_tail(path: str, keep: int) -> None:
    """Copy the last `keep` bytes to <path>.1 (atomically replacing it), then truncate the file in place. The job's
    O_APPEND descriptor keeps writing at the new end. Bytes appended between the copy and the truncate are lost."""
    tmp = path + ".1.tmp"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        src.seek(max(0, os.fstat(src.fileno()).st_size - keep))
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            dst.write(chunk)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path + ".1")
    os.truncate(path, 0)


class Ticker:
    def __init__(self, daemon):
        self.d = daemon
        self.last_sample_event_ms = 0
        self.last_slow_mono = -1e9
        self.last_retention_ms = 0
        self.last_scan_fail_mono = -1e9
        self.scan_failed = False
        # hardening (16 section 3)
        self.fail_streak: dict[str, int] = {}
        self.tick_failed = False
        self.full_tick_failed = False     # the last FULL tick had a failing phase: admission-only ticks stay closed
        self.last_wall_ms: int | None = None
        self.last_mono: float | None = None
        self.log_checked_mono: dict[str, float] = {}

    # ---- snapshot ---------------------------------------------------------------------------------------
    def build_snapshot(self, now_ms: int | None = None) -> Snapshot:
        d = self.d
        now = d.clock.now_ms() if now_ms is None else now_ms
        sample = d.last_sample
        scan = d.last_scan or ScanResult()
        rows = d.store.live_rows()
        dep_ids = {a for r in rows for a in json.loads(r["after_json"])}
        dep_states = d.store.states_of(dep_ids)
        prow = {r["id"]: r for r in d.store.projects()}
        waiters = d.start_waiters()
        jobs = []
        for r in rows:
            p = prow.get(r["project"])
            fp = scan.job_footprint[r["id"]] if r["id"] in scan.job_footprint else r["footprint_bytes"]
            if fp is None and r["state"] == "held" and r["id"] not in scan.job_footprint:
                # Admitted but never measured (spawning, or admitted after the last scan, as in every admission-only
                # tick). It has touched nothing yet: all of its reservation is pending. Treating it as "unreadable"
                # (0 pending) let the next admission pass the live check against a sample that predates it.
                fp = 0
            prof = d.residents.profile_of(r)
            jobs.append(JobView(
                id=r["id"], project=r["project"], name=r["name"], profile=r["profile"], state=r["state"],
                held_reason=r["held_reason"], queue=r["queue"], priority=r["priority"], checkpoint=r["checkpoint"],
                spawn=r["spawn"], gpu=r["gpu"], memory_bytes=r["memory_bytes"], cpu_cores=r["cpu_cores"],
                locks=frozenset(json.loads(r["locks_json"])), estimate_s=r["estimate_s"], limit_s=r["limit_s"],
                allow_spill=bool(r["allow_spill"]), after=tuple(json.loads(r["after_json"])),
                level_min=r["level_min"], ceiling_bytes=r["ceiling_bytes"], enqueued_ms=r["enqueued_ms"],
                eligible_since_ms=r["eligible_since_ms"], held_ms=r["held_ms"], started_ms=r["started_ms"],
                stop_requested_ms=r["stop_requested_ms"], stop_reason=r["stop_reason"], spilled=bool(r["spilled"]),
                checkpoint_failed=bool(r["checkpoint_failed"]), cancel_requested=bool(r["cancel_requested"]),
                hold=bool(r["hold"]), last_touch_ms=r["last_touch_ms"], switch_to=r["switch_to"],
                footprint_bytes=fp, grace_s=r["grace_s"], on_timeout=r["on_timeout"],
                start_waiter_parked=r["id"] in waiters,
                inhibited=bool(r["profile"]) and d.residents.is_inhibited(r["profile"]),
                project_present=bool(p["present"]) if p else False, project_weight=p["weight"] if p else 1,
                project_usage=p["usage"] if p else 0.0, attempt=r["attempt"], kill_sent=bool(r["kill_sent"]),
                footprint_fail_n=r["footprint_fail_n"], estimate_overrun=bool(r["estimate_overrun"]),
                over_limit=bool(r["over_limit"]), yield_requested=bool(r["yield_requested"]),
                ready=bool(r["ready"]), ceiling_noted=(r["id"], r["attempt"]) in d.ceiling_noted,
                idle_unload_s=prof.idle_unload_s if prof else None, abort=prof.abort if prof else None,
                listen_port=r["listen_port"], abort_unavail_since_ms=d.residents.abort_unavail.get(r["id"])))
        return Snapshot(
            now_ms=now, machine=d.machine, mem=sample, budget_bytes=d.budget_bytes, cpu_budget=d.cpu_budget,
            jobs=tuple(jobs), dep_states=dep_states, protective=d.protective.active, pressure=d.pressure,
            morning_latch=d.store.meta_get("morning_latch"), draining=d.draining,
            reservation_latches={k[len(reservations.LATCH_PREFIX):]: v
                                 for k, v in d.store.meta_prefix(reservations.LATCH_PREFIX).items()},
            last_pressure_action_ms=d.last_pressure_action_ms, baselines=dict(d.residents.baselines),
            swap_rise_gib_per_min=d.swap_rate, last_alarm_ms=d.last_alarm_ms)

    # ---- phase guard (16 section 3.1) ------------------------------------------------------------------------------
    def phase(self, name: str, fn, *args):
        """Run one phase. An exception (a policy or bookkeeping bug, or a sqlite OperationalError such as a full disk or
        a locked database) is logged and counted, the tick fails closed (no admission), and the loop carries on.
        Returns (ok, value)."""
        try:
            value = fn(*args)
        except Exception as e:
            self._phase_failed(name, e)
            return False, None
        self.fail_streak.pop(name, None)
        return True, value

    def _phase_failed(self, name: str, e: Exception) -> None:
        d = self.d
        self.tick_failed = True
        n = self.fail_streak[name] = self.fail_streak.get(name, 0) + 1
        if n <= 3 or n % 10 == 0:         # a persistent bug must not write 86,400 rows a day
            d.log.decision("tick_error", phase=name, streak=n, error=repr(e),
                           tb=traceback.format_exc().replace("\n", " | ")[-1500:])
        if n % PERSISTENT_AFTER == 0:
            d.log.decision("tick_error_persistent", phase=name, ticks=n, error=repr(e))

    # ---- the tick ----------------------------------------------------------------------------------------------------
    def run(self, admission_only: bool = False) -> None:
        d = self.d
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        now = d.clock.now_ms()
        self.tick_failed = False
        if d.last_sample is None:
            admission_only = False
        if not admission_only:
            self.phase("reload", d.reload_if_changed, now)
            self.phase("clock", self._clock_skew, now)
            self.phase("reconcile", self._reconcile, now)
            timings["reconcile"] = time.perf_counter() - t0
            self.phase("scan", self._scan, now)
            timings["scan"] = time.perf_counter() - t0
            self.phase("residents", d.residents.poll, now)
            self.phase("tcc", d.tcc.tick)
            if not self.scan_failed:
                self.phase("bookkeep", self._bookkeep, now)     # fail closed: no process table, nothing updates
            timings["bookkeep"] = time.perf_counter() - t0
            self.phase("job_logs", self._cap_job_logs, now)
        else:
            self.phase("reconcile", d.supervisor.reap_and_finalize, now)
        cfg = d.cfg
        ok, snap = self.phase("snapshot", self.build_snapshot, now)
        freeze = self.scan_failed or self.tick_failed or not ok or (admission_only and self.full_tick_failed)
        if ok and not admission_only and cfg.enforce:
            for name, mod in (("maintenance", maintenance), ("morning", morning), ("reservations", reservations),
                              ("pressure", pressure), ("preempt", preempt)):
                ok, res = self.phase(name, self._plan_phase, name, mod, snap, cfg, now)
                timings[name] = time.perf_counter() - t0
                if not ok:
                    freeze = True
                    ok2, snap2 = self.phase("snapshot", self.build_snapshot, now)
                    if not ok2:
                        snap = None
                        break
                    snap = snap2
                    continue
                pfreeze, snap = res
                freeze = freeze or pfreeze
        if snap is not None and cfg.enforce and not freeze and not self.tick_failed:
            self.phase("admit", self._admit, snap, cfg)
        if snap is not None and not admission_only and not self.scan_failed:
            failed = self.tick_failed
            self.phase("yield_apps", d.yield_apps.tick, snap)
            self.tick_failed = failed         # a yield_apps bug is logged (tick_error) but never freezes admission
        if not admission_only:
            self.full_tick_failed = self.tick_failed
        self.phase("cascade", self._cascade_dependencies)
        self.phase("notify", d.notify_parked)
        if not admission_only:
            self.phase("retention", self._retention, now)
            total = time.perf_counter() - t0
            if total > 0.2 and d.clock.mono() - self.last_slow_mono >= 60:
                self.last_slow_mono = d.clock.mono()
                d.log.decision("tick_slow", **{f"{k}_ms": round(v * 1000) for k, v in timings.items()},
                               total_ms=round(total * 1000))
            if d.debug:
                bad = d.store.check_invariants(d.budget_bytes, d.cfg.gpu_shared_max)
                if bad:
                    d.log.decision("invariant_violation", problems=bad)

    def _reconcile(self, now: int) -> None:
        sup = self.d.supervisor
        sup.reap_and_finalize(now)
        sup.check_identities(now)
        sup.expire_spawn_and_attach(now)
        self._expire_lost_waiters(now)

    def _scan(self, now: int) -> None:
        d = self.d
        self.scan_failed = True              # stays set if anything below raises: fail closed
        d.last_sample = d.platform.mem_sample(now)
        try:
            procs = d.platform.processes()
        except PlatformError as e:
            procs = None
            if d.clock.mono() - self.last_scan_fail_mono >= 60:
                self.last_scan_fail_mono = d.clock.mono()
                d.log.decision("scan_failed", error=str(e))
        if procs is not None:
            d.last_procs = procs
            gpu = d.platform.gpu_stats() if d.cfg.gpu_busy_foreign_pct is not None else None
            d.last_scan = d.scanner.classify(procs, d.store.held_jobs(), d.cfg, d.clock.mono(),
                                             gpu.device_util_pct if gpu else None)
        self.scan_failed = procs is None      # sticky until a scan succeeds (admission-only ticks reuse it)

    def _plan_phase(self, name: str, mod, snap: Snapshot, cfg, now: int):
        """Plan one phase and execute it. Returns (freeze_admission, snapshot)."""
        d = self.d
        plan = mod.plan(snap, cfg)
        if name == "preempt" and plan.reserve:
            d.preempt_reservation = plan.reserve
        if not plan.is_empty():
            self.execute(plan)
            snap = self.build_snapshot(now)
        return plan.freeze_admission, snap

    def _admit(self, snap: Snapshot, cfg) -> None:
        d = self.d
        plan = admit.plan(snap, cfg)
        self._note_reservation(snap, plan)
        d.last_blocked = plan.blocked
        if plan.admit:
            self._do_admit(plan.admit, snap)

    # ---- sleep / clock-jump safe timers (16 section 3.3) -----------------------------------------------------------
    def _clock_skew(self, now: int) -> None:
        """Wall time that moved differently from monotonic time (system sleep, an NTP step) must not make the grace and
        kill timers of a stopping or checkpointing job fire at once. Shift their start by the skew. `limit_s` keeps
        wall semantics on purpose (it includes sleep)."""
        d = self.d
        mono = d.clock.mono()
        last_wall, last_mono = self.last_wall_ms, self.last_mono
        self.last_wall_ms, self.last_mono = now, mono
        if last_wall is None or last_mono is None:
            return
        skew = (now - last_wall) - round((mono - last_mono) * 1000)
        if abs(skew) <= SKEW_THRESHOLD_MS:
            return
        n = d.store.shift_stop_timers(skew, now)
        d.log.decision("clock_skew", skew_ms=skew, shifted=n)

    # ---- job log cap (16 section 3.2) --------------------------------------------------------------------------------
    def _cap_job_logs(self, now: int) -> None:
        d = self.d
        cfg = d.cfg
        mono = d.clock.mono()
        live = set()
        for row in d.store.held_jobs():
            if row["spawn"] != "daemon":
                continue          # external jobs' logs belong to their launcher and are not managed
            jid = row["id"]
            live.add(jid)
            if mono - self.log_checked_mono.get(jid, -1e9) < LOG_CHECK_EVERY_S:
                continue
            self.log_checked_mono[jid] = mono
            jd = d.paths.job_dir(jid)
            for stream, col in (("stdout", "stdout_path"), ("stderr", "stderr_path")):
                if row[col]:
                    continue      # a user-chosen path: not ours to truncate
                path = os.path.join(jd, f"{stream}.log")
                try:
                    size = os.stat(path).st_size
                except OSError:
                    continue
                if size <= cfg.job_log_max_bytes:
                    continue
                keep = min(cfg.job_log_keep_bytes, size)
                try:
                    _truncate_keep_tail(path, keep)
                except OSError as e:
                    d.log.decision("job_log_truncate_failed", job=jid, stream=stream, error=str(e))
                    continue
                d.log.decision("job_log_truncated", job=jid, stream=stream, size_bytes=size, kept_bytes=keep)
        for jid in [j for j in self.log_checked_mono if j not in live]:
            del self.log_checked_mono[jid]

    # ---- phase 2: bookkeeping (one transaction) ------------------------------------------------------------------
    def _bookkeep(self, now: int) -> None:
        d = self.d
        cfg = d.cfg
        scan = d.last_scan
        sample = d.last_sample
        # pressure level (in memory ring buffer)
        d.swap_window.add(now, sample.swap_used_bytes)
        d.swap_rate = d.swap_window.rate_gib_per_min()
        lvl, why = pressure.level(sample, d.swap_rate, cfg)
        if lvl != d.pressure:
            d.log.decision("pressure_exit" if lvl == "normal" else "pressure_enter", level=lvl, why=why)
            if sample.swap_used_bytes is None:
                d.log.decision("swap_unknown")
        d.pressure = lvl
        d.pressure_why = why
        # protective mode
        gpu_running = any(r["gpu"] != "none" for r in d.store.held_jobs())
        entered, exited = d.protective.update(scan, cfg, d.clock.mono(), gpu_running)
        with d.store.batch():
            self._offenders(now, scan)
            if entered:
                d.log.decision("protective_enter", offenders=[o.path or f"port {o.port}" for o in d.protective.reasons])
                d.store.meta_set("protective", "1")
            if exited:
                d.log.decision("protective_exit")
                d.store.meta_set("protective", "0")
            held = d.store.held_jobs()
            for r in held:
                if r["id"] not in scan.job_footprint:
                    continue
                fp = scan.job_footprint[r["id"]]
                if fp is None:
                    d.store.set_flags(r["id"], footprint_fail_n=r["footprint_fail_n"] + 1)
                else:
                    peak = max(r["peak_footprint_bytes"] or 0, fp)
                    d.store.set_flags(r["id"], footprint_bytes=fp, footprint_fail_n=0, peak_footprint_bytes=peak)
            # fair share usage: decay then charge
            snap = self.build_snapshot(now)
            rows = {r["id"]: r for r in d.store.projects()}
            prev = {k: v["usage"] for k, v in rows.items()}
            last = {k: v["usage_t_ms"] or now for k, v in rows.items()}
            new = charge(prev, last, now, snap, cfg)
            for pid, u in new.items():
                if pid in rows:
                    d.store.set_project_usage(pid, u, now)
            # eligible_since_ms: first tick a job is eligible but does not fit
            if cfg.enforce:
                snap = self.build_snapshot(now)
                led = ledger(snap)
                dw = admit.day_waiting_ids(snap, cfg)
                for j in snap.jobs:
                    if j.state != "queued":
                        continue
                    ok, _ = admit.eligible(j, snap, cfg, dw)
                    fit = ok and fits(j, snap, cfg, led)[0]
                    if ok and not fit and j.eligible_since_ms is None:
                        d.store.set_flags(j.id, eligible_since_ms=now)
                    elif (not ok or fit) and j.eligible_since_ms is not None:
                        d.store.set_flags(j.id, eligible_since_ms=None)
        # periodic sample event
        if now - self.last_sample_event_ms >= SAMPLE_EVERY_MS:
            self.last_sample_event_ms = now
            self._sample_event(now)

    def _sample_event(self, now: int) -> None:
        d = self.d
        s = d.last_sample
        snap = self.build_snapshot(now)
        led = ledger(snap)
        g = d.platform.gpu_stats()
        pg = s.page_size
        d.log.decision("sample", level=s.level, free_gib=round(s.free_pages * pg / GiB, 2),
                       purgeable_gib=round(s.purgeable_pages * pg / GiB, 2),
                       file_backed_gib=round(s.file_backed_pages * pg / GiB, 2),
                       reclaimable_gib=round(s.reclaimable_bytes / GiB, 2),
                       swap_used_gib=None if s.swap_used_bytes is None else round(s.swap_used_bytes / GiB, 2),
                       budget_gib=round(d.budget_bytes / GiB, 2), reserved_gib=round(led.reserved_bytes / GiB, 2),
                       pending_gib=round(led.pending_bytes / GiB, 2), gpu_util=g.device_util_pct,
                       running=len(running(snap)))

    def _offenders(self, now: int, scan: ScanResult) -> None:
        d = self.d
        present = {}
        for o in list(scan.foreign_heavy) + list(scan.orphan_shims) + list(scan.noted):
            present[o.key] = o
        for key, o in present.items():
            oid = d.offender_ids.get(key)
            if oid is None:
                oid = d.store.offender_insert(pid=o.pid, path=o.path, port=o.port, cls=o.cls,
                                              footprint_bytes=o.footprint_bytes, now_ms=now)
                d.offender_ids[key] = oid
                d.log.decision("offender", pid=o.pid, path=o.path, port=o.port, cls=o.cls,
                               footprint_bytes=o.footprint_bytes)
            else:
                d.store.offender_touch(oid, o.footprint_bytes, now, o.pid)
        for key in [k for k in d.offender_ids if k not in present]:
            d.store.offender_clear(d.offender_ids.pop(key), now)

    # ---- executing plans ---------------------------------------------------------------------------------------------
    def execute(self, plan: Plan) -> None:
        d = self.d
        sup = d.supervisor
        for kind, job, body in plan.events:
            d.log.decision(kind, job=job, **body)
            if kind == "ceiling_exceeded" and job:
                row = d.store.get(job)
                if row:
                    d.ceiling_noted.add((job, row["attempt"]))
        for jid, flags in plan.flags:
            d.store.set_flags(jid, **flags)
        for k, v in plan.set_meta.items():
            d.store.meta_set(k, v)
            if k == "last_pressure_action_ms":
                d.last_pressure_action_ms = int(v)
        for jid in plan.kill:
            sup.escalate_kill(jid)
        for jid, reason in plan.spill:
            if d.store.mark_spilled(jid, reason):
                d.log.decision("spill", job=jid, reason=reason)
                d.notify_parked(jid)
                if d.cfg.night_window is not None and not windows.in_night(d.clock.now_ms(), d.cfg):
                    d.log.decision("blocking_day", job=jid)
        for jid, reason in plan.kill_stop:
            sup.stop(jid, reason)
        for jid, reason in plan.checkpoint:
            sup.checkpoint(jid, reason)
        for jid, reason in plan.stop:
            d.residents.request_unload(jid, reason)
        for a in plan.alarm:
            d.log.decision(a)
            d.last_alarm_ms = d.clock.now_ms()
            d.store.meta_set("last_alarm_ms", str(d.last_alarm_ms))

    def _note_reservation(self, snap: Snapshot, plan: Plan) -> None:
        d = self.d
        cur = plan.reserve
        if cur is None and d.preempt_reservation:
            still = [j for j in snap.jobs if j.id == d.preempt_reservation and j.state == "queued"]
            cur = d.preempt_reservation if still else None
            if cur is None:
                d.preempt_reservation = None
        if cur != d.reservation:
            if cur:
                d.log.decision("reservation_set", job=cur)
            elif d.reservation:
                d.log.decision("reservation_clear", job=d.reservation)
            d.reservation = cur

    def _do_admit(self, job_id: str, snap: Snapshot) -> None:
        d = self.d
        now = snap.now_ms
        row = d.store.get(job_id)
        if row is None:
            return
        led = ledger(snap)
        pos = {j.id: i + 1 for i, j in enumerate(admit.candidates(snap, d.cfg))}.get(job_id)
        info = dict(job=job_id, project=row["project"], queue=row["queue"], mem_bytes=row["memory_bytes"],
                    gpu=row["gpu"], reserved_before_bytes=led.reserved_bytes,
                    reserved_after_bytes=led.reserved_bytes + row["memory_bytes"],
                    reclaimable_bytes=snap.mem.reclaimable_bytes, pending_bytes=led.pending_bytes, position=pos,
                    reservation=d.reservation)
        if row["spawn"] == "external":
            token = ids.new_lease()
            if d.store.commit_admit(job_id, now, lease_hash=ids.lease_hash(token)):
                d.log.decision("admit", **info)
                d.deliver_lease(job_id, token)
        else:
            if d.store.commit_admit(job_id, now):
                d.log.decision("admit", **info)
                d.supervisor.spawn(job_id)
        d.admit_hint = True
        d.notify_parked(job_id)

    def _cascade_dependencies(self) -> None:
        d = self.d
        now = d.clock.now_ms()
        for _ in range(64):
            changed = False
            rows = [r for r in d.store.live_rows() if r["state"] in ("queued", "needs_estimate")]
            for r in rows:
                after = json.loads(r["after_json"])
                if not after:
                    continue
                states = d.store.states_of(after)
                if any(states.get(a) in ("failed", "cancelled", "lost") or a not in states for a in after):
                    if d.store.cancel_unstarted(r["id"], now, "dependency_failed"):
                        d.log.decision("exit", job=r["id"], state="cancelled", reason="dependency_failed")
                        changed = True
            if not changed:
                break

    def _expire_lost_waiters(self, now: int) -> None:
        d = self.d
        mono = d.clock.mono()
        parked = d.start_waiters()
        for jid in list(d.waiter_lost):
            if jid in parked:
                d.waiter_lost.pop(jid, None)
                continue
            row = d.store.get(jid)
            if row is None or row["state"] != "queued":
                d.waiter_lost.pop(jid, None)
                continue
            if (mono - d.waiter_lost[jid]) * 1000 >= d.cfg.attach_grace_ms:
                d.waiter_lost.pop(jid, None)
                if d.store.cancel_unstarted(jid, now, "waiter_gone"):
                    d.log.decision("exit", job=jid, state="cancelled", reason="waiter_gone")

    def _retention(self, now: int) -> None:
        d = self.d
        if now - self.last_retention_ms >= 86_400_000:
            self.last_retention_ms = now
            r = d.store.retention(now, d.cfg.retention_days)
            if any(r.values()):
                d.log.decision("retention", **r)
