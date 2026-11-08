"""RPC method table and handlers (05 section 4)."""

from __future__ import annotations

import json
import os
import time
import types

from gsched import __version__, history, ids
from gsched.checks import check_cwd, check_log_path, check_permissions, resolve_command
from gsched.errors import GschedError
from gsched.policy import admit as padmit
from gsched.policy.model import JobView
from gsched.spec import assign_queue, parse_spec
from gsched.status import build_status, explain_all, job_view
from gsched.units import GiB

TERMINAL = ("done", "failed", "cancelled", "lost")
DASHBOARD_METHODS = {"status", "list", "get", "logs", "events", "ping"}
MAX_GET_WAIT_S = 300
MAX_ESTIMATE_S = 30 * 86400


def _int(params: dict, key: str, *, default=None, lo=None, hi=None):
    v = params.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise GschedError("invalid", f"{key} must be an integer", field=key)
    if lo is not None and v < lo or hi is not None and v > hi:
        raise GschedError("invalid", f"{key} out of range", field=key)
    return v


def _str(params: dict, key: str, *, required=True):
    v = params.get(key)
    if v is None:
        if required:
            raise GschedError("invalid", f"{key} is required", field=key)
        return None
    if not isinstance(v, str):
        raise GschedError("invalid", f"{key} must be a string", field=key)
    return v


class Handlers:
    def __init__(self, daemon):
        self.d = daemon

    # ---- dispatch ------------------------------------------------------------------------------------------
    def dispatch(self, conn, method: str, params: dict, door: str):
        if door == "dashboard" and method not in DASHBOARD_METHODS:
            raise GschedError("forbidden", f"the dashboard door is read-only; {method!r} is not allowed")
        fn = getattr(self, "m_" + method, None)
        if fn is None or method.startswith("_"):
            raise GschedError("invalid", f"unknown method {method!r}", field="method")
        return fn(conn, params, door)

    # ---- views ---------------------------------------------------------------------------------------------
    def _offenders(self):
        return list(self.d.protective.reasons)

    def explain(self) -> dict:
        return explain_all(self.d.snapshot(), self.d.cfg, self._offenders())

    def view(self, row, *, door: str = "cli", full: bool = True, expl: dict | None = None) -> dict:
        d = self.d
        if expl is None and row["state"] in ("queued", "needs_estimate"):
            expl = self.explain()
        v = job_view(row, d.cfg, expl, full=full, paths=d.paths, daemon_state={"now_ms": d.clock.now_ms()})
        if row["state"] == "held" and d.last_scan is not None:
            fp = d.last_scan.job_footprint.get(row["id"])
            if fp is not None:
                v["footprint_gib"] = round(fp / GiB, 2)
        if door == "mcp" or door == "dashboard":
            v.pop("checkpoint_dir", None)
        return v

    def _row(self, job_id: str):
        if not isinstance(job_id, str) or not job_id:
            raise GschedError("invalid", "job is required", field="job")
        row = self.d.store.get(job_id)
        if row is None:
            raise GschedError("unknown_job", f"no such job {job_id}", job=job_id)
        return row

    # ---- submit ----------------------------------------------------------------------------------------------
    def m_submit(self, conn, params, door):
        d = self.d
        cfg = d.cfg
        spec = parse_spec(params.get("spec"), cfg, door="mcp" if door == "mcp" else "cli")
        if d.draining:
            raise GschedError("draining", "the daemon is draining and refuses new submissions")
        project = d.projects.get(spec.project)
        check_permissions(project, door, None)
        for fld, pth in (("stdout", spec.stdout_path), ("stderr", spec.stderr_path)):
            if pth:
                check_log_path(fld, pth, d.tcc)
        cwd = spec.cwd or project.default_cwd
        if cwd is None and spec.spawn == "daemon":
            raise GschedError("invalid", "cwd is required (the CLI fills it in; MCP clients must send it, "
                              "or the project file must set default_cwd)", field="cwd")
        if spec.memory_bytes > d.budget_bytes:
            raise GschedError("too_big", f"memory_gib {spec.memory_bytes / GiB:g} exceeds the budget of "
                              f"{d.budget_bytes / GiB:.1f} GiB; it could never run", field="resources.memory_gib",
                              budget_gib=round(d.budget_bytes / GiB, 2))
        if spec.cpu_cores > d.cpu_budget:
            raise GschedError("too_big", f"cpu_cores {spec.cpu_cores} exceeds cpu_budget {d.cpu_budget}",
                              field="resources.cpu_cores", cpu_budget=d.cpu_budget)
        if cwd is not None:
            cwd = check_cwd(cwd, d.tcc, external=spec.spawn == "external")
        resolved0 = None
        if spec.spawn == "daemon":
            path_env = spec.env.get("PATH") or cfg.base_path
            resolved0 = resolve_command(spec.command, path_env, cwd, d.tcc)
            check_permissions(project, door, resolved0)
        if spec.after:
            states = d.store.states_of(spec.after)
            unknown = [a for a in spec.after if a not in states]
            if unknown:
                raise GschedError("invalid", f"unknown job ids in after: {', '.join(unknown)}", field="after",
                                  unknown=unknown)
        if spec.idempotency_key:
            existing = d.store.find_idempotent(spec.project, spec.idempotency_key)
            if existing is not None:
                return {"job": existing["id"], "state": existing["state"], "queue": existing["queue"],
                        "warnings": [], "idempotent": True}
        queue, state = assign_queue(spec, cfg)
        now = d.clock.now_ms()
        if not cfg.enforce:
            return self._observe(spec, queue, state, cwd, now)
        job_id = d.store.insert_job(spec, queue=queue, state=state, door=door, now_ms=now,
                                    client_pid=conn.peer_pid, cwd=cwd)
        d.log.decision("submit", job=job_id, project=spec.project, name=spec.name, queue=queue,
                       priority=spec.priority, gpu=spec.gpu, mem_bytes=spec.memory_bytes,
                       checkpoint=spec.checkpoint, door=door)
        warnings = history.submit_warnings(d.store, spec, d.machine)
        d.admit_hint = True
        return {"job": job_id, "state": state, "queue": queue, "warnings": warnings}

    def _observe(self, spec, queue, state, cwd, now):
        d = self.d
        snap = d.snapshot()
        prow = {r["id"]: r for r in d.store.projects()}.get(spec.project)
        jv = JobView(
            id="(new)", project=spec.project, name=spec.name, state=state, held_reason=None, queue=queue,
            priority=spec.priority, checkpoint=spec.checkpoint, spawn=spec.spawn, gpu=spec.gpu,
            memory_bytes=spec.memory_bytes, cpu_cores=spec.cpu_cores, locks=frozenset(spec.locks),
            estimate_s=spec.estimate_s, limit_s=spec.limit_s, allow_spill=spec.allow_spill, after=spec.after,
            level_min=spec.level_min, ceiling_bytes=spec.ceiling_bytes, enqueued_ms=now,
            start_waiter_parked=True, project_weight=prow["weight"] if prow else 1,
            project_usage=prow["usage"] if prow else 0.0)
        # observe mode evaluates as if enforcing
        import dataclasses
        cfg2 = dataclasses.replace(d.cfg, enforce=True)
        would = padmit.explain_new(jv, snap, cfg2)
        if would.get("blocked_by"):
            from gsched.status import _why_one
            from gsched.policy.fit import ledger
            led = ledger(snap)
            would["why"] = "; ".join(_why_one(c, jv, snap, d.cfg, led, {}, [], None, self._offenders())
                                     for c in would["blocked_by"])
        return {"state": "observe", "job": None, "would": would,
                "hint": "gsched is in observe mode (enforce=false); nothing was queued. "
                        "Enable with: gsched config set enforce true"}

    # ---- get / list / wait ---------------------------------------------------------------------------------------
    def m_get(self, conn, params, door):
        d = self.d
        row = self._row(params.get("job"))
        wait_s = params.get("wait_s")
        if wait_s is not None:
            if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)) or wait_s < 0:
                raise GschedError("invalid", "wait_s must be a non-negative number", field="wait_s")
            cap = d.cfg.mcp_max_wait_s if door == "mcp" else MAX_GET_WAIT_S
            wait_s = min(wait_s, cap)
            if wait_s > 0 and row["state"] not in TERMINAL:
                d.park(conn, "get_wait", row["id"], deadline_mono=d.clock.mono() + wait_s)
                return None
        return self.view(row, door=door)

    def m_list(self, conn, params, door):
        d = self.d
        st = params.get("state")
        if isinstance(st, str):
            st = [st]
        if st is not None and (not isinstance(st, list) or not all(isinstance(x, str) for x in st)):
            raise GschedError("invalid", "state must be a string or list of strings", field="state")
        limit = _int(params, "limit", default=100, lo=1, hi=1000)
        since = _int(params, "since_ms", lo=0)
        project = _str(params, "project", required=False)
        rows = d.store.list_jobs(states=st, project=project, limit=limit, since_ms=since)
        expl = self.explain() if any(r["state"] in ("queued", "needs_estimate") for r in rows) else {}
        return {"jobs": [self.view(r, door=door, full=False, expl=expl) for r in rows]}

    def m_wait(self, conn, params, door):
        d = self.d
        row = self._row(params.get("job"))
        until = params.get("until", "exit")
        if until not in ("exit", "start"):
            raise GschedError("invalid", "until must be exit or start", field="until")
        if until == "start":
            if row["spawn"] != "external":
                raise GschedError("invalid", "wait --until start applies only to spawn=external jobs", field="until")
            if row["state"] in TERMINAL or row["state"] == "held":
                return self.view(row, door=door)
            d.park(conn, "wait_start", row["id"])
            d.admit_hint = True
            return None
        if row["state"] in TERMINAL:
            return self.view(row, door=door)
        d.park(conn, "wait_exit", row["id"])
        return None

    # ---- cancel ---------------------------------------------------------------------------------------------------
    def m_cancel(self, conn, params, door):
        d = self.d
        st, sup = d.store, d.supervisor
        if params.get("lease") is not None:
            row = st.by_lease(params["lease"])
            if row is None:
                raise GschedError("bad_lease", "unknown lease")
        else:
            row = self._row(params.get("job"))
        terminate = bool(params.get("terminate", False))
        end_reason = params.get("end_reason")
        if end_reason not in (None, "max_wait"):
            raise GschedError("invalid", "end_reason may only be max_wait", field="end_reason")
        if row["state"] in TERMINAL:
            raise GschedError("bad_state", f"job is already {row['state']}", state=row["state"])
        if terminate and door == "mcp":
            proj = d.projects.find(row["project"])
            if proj is None or not proj.mcp_terminate:
                raise GschedError("forbidden", "terminate is not enabled for this project over MCP "
                                  '(set "mcp_terminate": true in the project file)')
        jid, now = row["id"], d.clock.now_ms()
        action, message = "cancelled", None
        reason = end_reason or "cancelled"
        if row["state"] in ("needs_estimate", "queued"):
            st.cancel_unstarted(jid, now, reason)
            d.log.decision("exit", job=jid, state="cancelled", reason=reason)
            if end_reason == "max_wait":
                d.log.decision("exec_max_wait", job=jid, waited_s=round((now - row["enqueued_ms"]) / 1000))
        elif row["spawn"] == "external":
            if terminate:
                raise GschedError("forbidden", "external jobs are owned by their launcher")
            if row["held_reason"] == "await_attach" and row["pid"] is None:
                st.finish(jid, now, state="cancelled", end_reason=reason)
                d.log.decision("exit", job=jid, state="cancelled", reason=reason)
                if end_reason == "max_wait":
                    d.log.decision("exec_max_wait", job=jid, waited_s=round((now - row["enqueued_ms"]) / 1000))
            else:
                st.mark_cancel_requested(jid)
                action = "cancel_requested_no_signal"
                message = "external jobs are never signaled; the launcher sees cancel_requested via `get`"
        else:
            hr = row["held_reason"]
            st.mark_cancel_requested(jid)
            if hr == "spawning":
                if jid in sup.pending:
                    sup.abort_spawn(jid, "cancelled")
                action = "cancelling"
            elif hr == "stopping":
                action = "stopping"
            elif row["checkpoint"] == "restartable":
                sup.stop(jid, "cancel")
                action = "stopping"
            elif terminate:
                sup.stop(jid, "terminate")
                action = "terminating"
            elif row["checkpoint"] == "cooperative" and hr == "live" and not row["spilled"]:
                sup.checkpoint(jid, "cancel")
                action = "checkpoint_requested"
            elif row["checkpoint"] == "cooperative" and hr == "checkpointing":
                action = "cancel_requested"
            else:       # atomic, or spilled
                action = "cancel_requested_no_signal"
                message = ("atomic jobs are not signaled; use terminate=true to kill it (unsaved work is lost)")
        d.admit_hint = True
        d.notify_parked(jid)
        new = st.get(jid)
        out = {"job": jid, "state": new["state"], "action": action}
        if message:
            out["message"] = message
        return out

    # ---- estimate / logs --------------------------------------------------------------------------------------------
    def m_estimate(self, conn, params, door):
        d = self.d
        row = self._row(params.get("job"))
        est = params.get("estimate_s")
        if isinstance(est, bool) or not isinstance(est, int) or not (0 < est <= MAX_ESTIMATE_S):
            raise GschedError("invalid", "estimate_s must be an integer in 1..2592000", field="estimate_s")
        if row["state"] not in ("needs_estimate", "queued"):
            raise GschedError("bad_state", f"cannot set an estimate on a {row['state']} job", state=row["state"])
        ns = types.SimpleNamespace(checkpoint=row["checkpoint"], estimate_s=est, queue_requested=row["queue_requested"])
        queue, _ = assign_queue(ns, d.cfg)
        d.store.set_estimate(row["id"], est, queue or "day", d.clock.now_ms())
        d.admit_hint = True
        return self.view(d.store.get(row["id"]), door=door)

    def m_logs(self, conn, params, door):
        d = self.d
        row = self._row(params.get("job"))
        stream = params.get("stream", "stdout")
        if stream not in ("stdout", "stderr"):
            raise GschedError("invalid", "stream must be stdout or stderr", field="stream")
        cap = 16384 if door in ("mcp", "dashboard") else 1024 * 1024
        tail = _int(params, "tail_bytes", default=8192, lo=1, hi=cap)
        offset = _int(params, "offset", lo=0)
        path = (row["stdout_path"] if stream == "stdout" else row["stderr_path"]) or \
            os.path.join(d.paths.job_dir(row["id"]), f"{stream}.log")
        try:
            size = os.path.getsize(path)
        except OSError:
            return {"data": "", "offset": offset or 0, "eof": True, "size": 0}
        start = max(0, size - tail) if offset is None else min(offset, size)
        want = size - start if offset is None else min(tail, size - start)
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read(want)
        end = start + len(raw)
        return {"data": raw.decode("utf-8", "replace"), "offset": end, "eof": end >= size, "size": size}

    # ---- status / ping / reload / events / suggest / doctor -----------------------------------------------------------
    def m_status(self, conn, params, door):
        return build_status(self.d, brief=bool(params.get("brief")), dashboard=door == "dashboard")

    def m_ping(self, conn, params, door):
        d = self.d
        return {"version": __version__, "pid": os.getpid(), "enforce": d.cfg.enforce,
                "uptime_s": round(d.clock.mono() - d.start_mono, 1)}

    def m_reload(self, conn, params, door):
        self.d.reload_all()
        return {"ok": True}

    def m_events(self, conn, params, door):
        d = self.d
        kinds = params.get("kinds")
        excl = params.get("exclude_kinds")
        for k, v in (("kinds", kinds), ("exclude_kinds", excl)):
            if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
                raise GschedError("invalid", f"{k} must be a list of strings", field=k)
        since = _int(params, "since_ms", default=d.clock.now_ms() - 6 * 3600 * 1000, lo=0)
        limit = _int(params, "limit", default=2000, lo=1, hi=10000)
        job = _str(params, "job", required=False)
        evs = d.store.events(kinds=kinds, exclude_kinds=excl, since_ms=since, job=job, limit=limit,
                             newest_first=bool(params.get("newest_first")))
        return {"events": evs}

    def m_suggest(self, conn, params, door):
        return history.suggest(self.d.store, _str(params, "project"), _str(params, "name", required=False))

    def m_doctor(self, conn, params, door):
        from gsched.doctor import daemon_checks
        return {"checks": daemon_checks(self.d)}

    # ---- drain ----------------------------------------------------------------------------------------------------------
    def m_drain(self, conn, params, door):
        d = self.d
        d.draining = True
        d.store.meta_set("drain", "1")
        d.log.decision("drain", on=True)
        for r in d.store.held_jobs():
            if r["profile"] and not r["hold"] and r["checkpoint"] == "restartable" and r["held_reason"] == "live":
                d.residents.request_unload(r["id"], "drain")
        return {"draining": True}

    def m_undrain(self, conn, params, door):
        d = self.d
        d.draining = False
        d.store.meta_set("drain", "0")
        d.log.decision("drain", on=False)
        d.admit_hint = True
        return {"draining": False}

    # ---- external launchers ------------------------------------------------------------------------------------------------
    def _lease_row(self, params):
        row = self.d.store.by_lease(params.get("lease"))
        if row is None or row["spawn"] != "external":
            raise GschedError("bad_lease", "unknown or invalid lease")
        return row

    def m_attach(self, conn, params, door):
        d = self.d
        row = self._lease_row(params)
        pid = params.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise GschedError("invalid", "pid must be an integer", field="pid")
        if row["state"] != "held" or row["held_reason"] != "await_attach" or row["pid"] is not None:
            raise GschedError("bad_state", "job is not awaiting attach", state=row["state"])
        if pid in (0, 1) or pid == os.getpid() or pid < 0:
            raise GschedError("invalid", "refusing to attach pid 0, 1 or the daemon itself", field="pid")
        info = d.platform.proc(pid)
        if info is None or info.uid != os.getuid():
            raise GschedError("invalid", "pid does not exist or is not owned by the daemon's user", field="pid")
        from gsched.platform import JobIdentity
        ident = JobIdentity(pid, info.start_sec, info.start_usec, info.path, info.pgid)
        if not d.store.begin_child(row["id"], ident, d.clock.now_ms(), ready=True):
            raise GschedError("bad_state", "attach failed: job state changed")
        in_place = pid == getattr(conn, "peer_pid", None)
        if in_place:
            # `gsched exec --replace`: the caller attached ITSELF and is about to exec the command. Nobody will call
            # `finish`, so the exit is unobservable; record that so the pid going away ends the job as
            # done/exit_unobserved rather than a `lost` warning (live 2026-11-08).
            d.store.meta_set(f"in_place:{row['id']}", "1")
        d.log.decision("gate_released", job=row["id"], pid=pid, path=info.path, external=True, in_place=in_place)
        d.notify_parked(row["id"])
        return {"job": row["id"], "state": "held", "held_reason": "live"}

    def m_finish(self, conn, params, door):
        d = self.d
        row = self._lease_row(params)
        if row["state"] in TERMINAL:
            return {"job": row["id"], "state": row["state"]}
        if row["state"] != "held" or row["pid"] is None:
            raise GschedError("bad_state", "finish requires an attached job (use release otherwise)")
        code = _int(params, "exit_code")
        sig = _int(params, "signal")
        if code is None and sig is None:
            raise GschedError("invalid", "exit_code or signal is required", field="exit_code")
        state = d.store.finish(row["id"], d.clock.now_ms(), exit_code=code, exit_signal=sig)
        d.log.decision("exit", job=row["id"], state=state, code=code, sig=sig, external=True)
        d.admit_hint = True
        d.notify_parked(row["id"])
        return {"job": row["id"], "state": state}

    def m_release(self, conn, params, door):
        d = self.d
        row = self._lease_row(params)
        if row["state"] in TERMINAL:
            return {"job": row["id"], "state": row["state"]}
        now = d.clock.now_ms()
        if row["pid"] is None:
            state = d.store.finish(row["id"], now, state="cancelled", end_reason="released_unstarted")
        elif d.platform.pid_alive(row["pid"]):
            raise GschedError("still_running", "the attached pid is alive; call finish when it exits")
        else:
            d.store.mark_lost(row["id"], now, "exit_unknown")
            state = "lost"
        d.log.decision("exit", job=row["id"], state=state, reason="release", external=True)
        d.admit_hint = True
        d.notify_parked(row["id"])
        return {"job": row["id"], "state": state}

    # ---- residents (08) --------------------------------------------------------------------------------------------------------
    def m_ensure(self, conn, params, door):
        return self.d.residents.ensure(_str(params, "profile"), door)

    def m_inhibit(self, conn, params, door):
        return self.d.residents.set_inhibit(_str(params, "profile"), True)

    def m_uninhibit(self, conn, params, door):
        return self.d.residents.set_inhibit(_str(params, "profile"), False)

    def m_hold(self, conn, params, door):
        return self.d.residents.set_hold(_str(params, "profile"), True)

    def m_unhold(self, conn, params, door):
        return self.d.residents.set_hold(_str(params, "profile"), False)

    # ---- retention -------------------------------------------------------------------------------------------------------
    def m_gc(self, conn, params, door):
        d = self.d
        older = _int(params, "older_than_s", lo=1)
        days = (older / 86400) if older else d.cfg.retention_days
        return d.store.retention(d.clock.now_ms(), days)
