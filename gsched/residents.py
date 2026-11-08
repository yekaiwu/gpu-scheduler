"""Resident profiles: ensure, health/slots probes, idle unload, switch, abort lines (08).

The daemon is single-threaded, so probes are tiny non-blocking HTTP/1.0 GETs driven by the selector.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
import urllib.parse
from dataclasses import dataclass, field

from gsched.checks import check_cwd, resolve_command
from gsched.errors import GschedError
from gsched.policy import admit as padmit
from gsched.policy.model import Baseline, JobView
from gsched.projects import Profile, ProfileRegistry

PROBE_TIMEOUT_S = 1.0
HEALTH_EVERY_S = 10.0
URGENT_REASONS = {"pressure", "protective", "abort", "cancel", "terminate"}


@dataclass
class Probe:
    kind: str                 # health | slots
    job_id: str
    sock: socket.socket
    request: bytes
    started_mono: float
    state: str = "connecting"
    buf: bytearray = field(default_factory=bytearray)
    done: bool = False


@dataclass
class StopPending:
    reason: str
    since_ms: int
    slots_unavailable_logged: bool = False


class Residents:
    def __init__(self, daemon):
        self.d = daemon
        self.registry: ProfileRegistry | None = None
        self.baselines: dict[str, Baseline] = {}
        self.abort_unavail: dict[str, int] = {}
        self.probes: dict[tuple[str, str], Probe] = {}
        self._bearer_refused: set = set()
        self.last_health_mono: dict[str, float] = {}
        self.health_fail_n: dict[str, int] = {}
        self.health_lost_logged: set = set()
        self.stop_pending: dict[str, StopPending] = {}
        self._inhibited: set = set()
        self._holds: set = set()

    # ---- loading ------------------------------------------------------------------------------------------
    def load_profiles(self) -> None:
        d = self.d
        self.registry = ProfileRegistry(d.paths.profiles, d.paths.env_file, d.projects, lambda: d.cfg)
        self._report(self.registry.reload_if_changed(force=True))
        meta = d.store.meta_prefix("inhibit:")
        self._inhibited = {k.split(":", 1)[1] for k, v in meta.items() if v == "1"}
        meta = d.store.meta_prefix("profile_hold:")
        self._holds = {k.split(":", 1)[1] for k, v in meta.items() if v == "1"}

    def reload_if_changed(self, force: bool = False) -> None:
        if self.registry is not None:
            self._report(self.registry.reload_if_changed(force))

    def _report(self, new_errors) -> None:
        for name, err in new_errors:
            self.d.log.decision("profile_invalid", profile=name, error=err)

    def profile_of(self, row) -> Profile | None:
        name = row["profile"]
        if not name or self.registry is None:
            return None
        return self.registry.profiles.get(name)

    def is_inhibited(self, name: str) -> bool:
        return name in self._inhibited

    def health_applies(self, row) -> bool:
        p = self.profile_of(row)
        return bool(p and p.health_url)

    # ---- ensure / inhibit / hold --------------------------------------------------------------------------------------
    def _profile(self, name: str) -> Profile:
        p = self.registry.profiles.get(name) if self.registry else None
        if p is None:
            err = self.registry.errors.get(name) if self.registry else None
            raise GschedError("unknown_profile", f"no usable profile {name!r}" + (f": {err}" if err else ""),
                              profile=name, **({"error": err} if err else {}))
        return p

    def _view(self, name: str, row, view: str | None = None, **extra) -> dict:
        d = self.d
        out: dict = {"profile": name}
        if row is not None:
            row = d.store.get(row["id"])
            if view is None:
                if row["state"] == "held":
                    view = "running" if (row["held_reason"] == "live" and row["ready"]) else "starting"
                elif row["state"] == "queued":
                    view = "queued"
                else:
                    view = row["state"]
            out.update({"job": row["id"], "state": row["state"], "held_reason": row["held_reason"],
                        "ready": bool(row["ready"]), "attempt": row["attempt"]})
            if row["state"] == "queued":
                e = d.handlers.explain().get(row["id"], {})
                out.update(blocked_by=e.get("blocked_by", []), why=e.get("why", ""))
        out["view"] = view
        out.update(extra)
        return out

    def ensure(self, name: str, door: str) -> dict:
        d = self.d
        p = self._profile(name)
        now = d.clock.now_ms()
        if not p.admit:
            return {"profile": name, "view": "refused", "reason": p.reason or "admit is false"}
        if not d.cfg.enforce:
            snap = d.snapshot()
            sp = p.spec
            jv = JobView(id="(new)", project=sp.project, name=name, state="queued", queue="day",
                         priority=sp.priority, checkpoint="restartable", spawn="daemon", gpu=sp.gpu,
                         memory_bytes=sp.memory_bytes, cpu_cores=sp.cpu_cores, locks=frozenset(sp.locks),
                         enqueued_ms=now, profile=name)
            import dataclasses
            would = padmit.explain_new(jv, snap, dataclasses.replace(d.cfg, enforce=True))
            return {"profile": name, "view": "observe", "would": would,
                    "hint": "gsched is in observe mode (enforce=false); nothing was queued"}
        if name in self._inhibited:
            return {"profile": name, "view": "inhibited"}
        if d.draining:
            return {"profile": name, "view": "inhibited", "reason": "draining"}
        row = d.store.live_profile_row(name)
        if row is not None:
            d.store.set_flags(row["id"], last_touch_ms=now)
            if row["switch_to"]:            # last caller wins: revert a pending switch away from this profile
                other = d.store.live_profile_row(row["switch_to"])
                d.store.set_flags(row["id"], switch_to=None)
                if other is not None and other["state"] == "queued":
                    d.store.cancel_unstarted(other["id"], now, "switch_reverted")
            return self._view(name, row)
        spec = p.spec
        if spec.memory_bytes > d.budget_bytes:
            raise GschedError("too_big", f"profile {name} needs more than the memory budget")
        cwd = check_cwd(spec.cwd, d.tcc) if spec.cwd else None
        resolve_command(spec.command, spec.env.get("PATH") or d.cfg.base_path, cwd, d.tcc)
        same = [r for r in d.store.held_jobs() if r["project"] == p.project and r["profile"] not in (None, name)
                and r["checkpoint"] == "restartable" and r["spawn"] == "daemon"]
        row_id = d.store.insert_job(spec, queue="day", state="queued", door="profile", now_ms=now, profile=name,
                                    hold=(p.hold or name in self._holds), last_touch_ms=now,
                                    listen_port=p.listen_port, cwd=cwd)
        d.log.decision("submit", job=row_id, project=spec.project, name=name, queue="day", priority=spec.priority,
                       gpu=spec.gpu, mem_bytes=spec.memory_bytes, checkpoint="restartable", door="profile")
        for s in same:
            d.store.set_flags(s["id"], switch_to=name)
        d.admit_hint = True
        return self._view(name, d.store.get(row_id))

    def set_inhibit(self, name: str, on: bool) -> dict:
        self._profile(name)
        if on:
            self._inhibited.add(name)
            self.d.store.meta_set(f"inhibit:{name}", "1")
        else:
            self._inhibited.discard(name)
            self.d.store.meta_delete(f"inhibit:{name}")
            self.d.admit_hint = True
        return {"profile": name, "inhibited": on}

    def set_hold(self, name: str, on: bool) -> dict:
        self._profile(name)
        d = self.d
        if on:
            self._holds.add(name)
            d.store.meta_set(f"profile_hold:{name}", "1")
        else:
            self._holds.discard(name)
            d.store.meta_delete(f"profile_hold:{name}")
        row = d.store.live_profile_row(name)
        if row is not None:
            d.store.set_flags(row["id"], hold=int(on))
        return {"profile": name, "hold": on}

    def status_list(self) -> list:
        out = []
        if self.registry is None:
            return out
        for name, p in sorted(self.registry.profiles.items()):
            row = self.d.store.live_profile_row(name)
            out.append({"name": name, "project": p.project, "inhibited": name in self._inhibited,
                        "hold": name in self._holds or p.hold, "admit": p.admit,
                        "job": row["id"] if row else None, "state": row["state"] if row else None,
                        "ready": bool(row["ready"]) if row else False})
        for name, err in sorted(self.registry.errors.items()):
            out.append({"name": name, "error": err})
        return out

    # ---- unloading (08 section 5) ---------------------------------------------------------------------------------------
    def request_unload(self, job_id: str, reason: str) -> bool:
        d = self.d
        row = d.store.get(job_id)
        if row is None or row["state"] != "held" or row["spawn"] != "daemon" or row["checkpoint"] != "restartable":
            # Policy stops apply to restartable jobs we spawned only (ground rule 2). Never fall through to a
            # signal for anything else; explicit terminations call supervisor.stop directly.
            return False
        if row["held_reason"] != "live":
            return False
        if job_id in self.stop_pending:
            return True
        p = self.profile_of(row)
        if p is not None and p.slots_url and reason not in URGENT_REASONS:
            self.stop_pending[job_id] = StopPending(reason, d.clock.now_ms())
            return True
        self.stop_pending.pop(job_id, None)
        return d.supervisor.stop(job_id, reason)

    # ---- the per-tick poll ---------------------------------------------------------------------------------------------------
    def poll(self, now_ms: int) -> None:
        d = self.d
        mono = d.clock.mono()
        self._expire_probes(mono)
        s = d.last_sample
        live_ids = set()
        for row in d.store.held_jobs():
            if row["profile"] is None or row["spawn"] != "daemon":
                continue
            jid = row["id"]
            live_ids.add(jid)
            p = self.profile_of(row)
            if p is None or row["held_reason"] != "live":
                continue
            if row["ready"] and jid not in self.baselines and s is not None:
                self._set_baseline(jid, s)
            if p.health_url and (jid, "health") not in self.probes:
                due = (not row["ready"]) or mono - self.last_health_mono.get(jid, -1e9) >= HEALTH_EVERY_S
                if due:
                    self.last_health_mono[jid] = mono
                    self._start_probe("health", jid, p.health_url, p)
            # abort lines whose sample is unreadable
            a = p.abort
            if a.armed and row["ready"] and s is not None:
                missing = (a.level_below is not None and s.level is None) or \
                          (a.swap_rise_gib is not None and s.swap_used_bytes is None)
                if missing:
                    self.abort_unavail.setdefault(jid, now_ms)
                else:
                    self.abort_unavail.pop(jid, None)
        for jid in list(self.baselines):
            if jid not in live_ids:
                self.baselines.pop(jid, None)
        for jid in list(self.abort_unavail):
            if jid not in live_ids:
                self.abort_unavail.pop(jid, None)
        self._poll_stop_pending(now_ms, mono)

    def _set_baseline(self, jid: str, s) -> None:
        self.baselines[jid] = Baseline(s.swap_used_bytes, s.level, s.reclaimable_bytes)

    def _poll_stop_pending(self, now_ms: int, mono: float) -> None:
        d = self.d
        for jid, sp in list(self.stop_pending.items()):
            row = d.store.get(jid)
            if row is None or row["state"] != "held" or row["held_reason"] != "live":
                self.stop_pending.pop(jid, None)
                continue
            p = self.profile_of(row)
            grace_over = now_ms - sp.since_ms >= d.cfg.preempt_grace_ms
            if p is None or not p.slots_url or grace_over:
                self.stop_pending.pop(jid, None)
                d.supervisor.stop(jid, sp.reason)
                continue
            if (jid, "slots") not in self.probes:
                self._start_probe("slots", jid, p.slots_url, p)

    # ---- probes ------------------------------------------------------------------------------------------------------------------
    def _start_probe(self, kind: str, jid: str, url: str, p: Profile) -> None:
        d = self.d
        u = urllib.parse.urlsplit(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        headers = [f"GET {path} HTTP/1.0", f"Host: {u.netloc}"]
        if p.health_bearer_file and d.tcc.root_for(p.health_bearer_file) is not None:
            # opening a file inside a TCC-protected folder would block this thread behind a consent prompt
            if jid not in self._bearer_refused:
                self._bearer_refused.add(jid)
                d.log.decision("health_bearer_refused", job=jid, why="bearer file is in a TCC-protected root")
        elif p.health_bearer_file:
            try:
                with open(p.health_bearer_file) as fh:
                    headers.append("Authorization: Bearer " + fh.read().strip())   # never logged or stored
            except OSError:
                pass
        req = ("\r\n".join(headers) + "\r\n\r\n").encode()
        try:
            fam = socket.AF_INET6 if ":" in host else socket.AF_INET
            sock = socket.socket(fam, socket.SOCK_STREAM)
            sock.setblocking(False)
            sock.connect_ex((host, port))
        except OSError:
            self._finish_probe_error(kind, jid)
            return
        pr = Probe(kind, jid, sock, req, d.clock.mono())
        self.probes[(jid, kind)] = pr
        d.sel.register(sock, selectors.EVENT_WRITE, _ProbeKey(pr))

    def on_probe_event(self, pr: Probe, mask: int) -> None:
        d = self.d
        if pr.done:
            return
        try:
            if pr.state == "connecting" and mask & selectors.EVENT_WRITE:
                err = pr.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err:
                    return self._end_probe(pr, None)
                pr.sock.send(pr.request)
                pr.state = "reading"
                d.sel.modify(pr.sock, selectors.EVENT_READ, _ProbeKey(pr))
            elif pr.state == "reading" and mask & selectors.EVENT_READ:
                chunk = pr.sock.recv(65536)
                if not chunk:
                    return self._end_probe(pr, bytes(pr.buf))
                pr.buf += chunk
                if len(pr.buf) > 65536:
                    return self._end_probe(pr, bytes(pr.buf))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._end_probe(pr, None)

    def _expire_probes(self, mono: float) -> None:
        for pr in list(self.probes.values()):
            if mono - pr.started_mono >= PROBE_TIMEOUT_S:
                self._end_probe(pr, None)

    def _end_probe(self, pr: Probe, raw: bytes | None) -> None:
        d = self.d
        pr.done = True
        self.probes.pop((pr.job_id, pr.kind), None)
        try:
            d.sel.unregister(pr.sock)
        except (KeyError, ValueError):
            pass
        pr.sock.close()
        status, body = None, b""
        if raw:
            head, _, body = raw.partition(b"\r\n\r\n")
            try:
                status = int(head.split(b"\r\n", 1)[0].split()[1])
            except (IndexError, ValueError):
                status = None
        if pr.kind == "health":
            self._health_result(pr.job_id, status)
        else:
            self._slots_result(pr.job_id, status, body)

    def _finish_probe_error(self, kind: str, jid: str) -> None:
        if kind == "health":
            self._health_result(jid, None)
        else:
            self._slots_result(jid, None, b"")

    def _health_result(self, jid: str, status: int | None) -> None:
        d = self.d
        row = d.store.get(jid)
        if row is None or row["state"] != "held":
            return
        ok = status is not None and 200 <= status < 300
        if ok:
            self.health_fail_n[jid] = 0
            self.health_lost_logged.discard(jid)
            if not row["ready"]:
                d.store.set_flags(jid, ready=1)
                if d.last_sample is not None:
                    self._set_baseline(jid, d.last_sample)
                d.log.decision("ready", job=jid)
                d.notify_parked(jid)
        elif row["ready"]:
            n = self.health_fail_n.get(jid, 0) + 1
            self.health_fail_n[jid] = n
            if n >= 3 and jid not in self.health_lost_logged:
                self.health_lost_logged.add(jid)
                d.log.decision("health_lost", job=jid)

    def _slots_result(self, jid: str, status: int | None, body: bytes) -> None:
        d = self.d
        sp = self.stop_pending.get(jid)
        if sp is None:
            return
        busy = None
        if status is not None and 200 <= status < 300:
            try:
                data = json.loads(body.decode("utf-8", "replace"))
                if isinstance(data, list):
                    busy = any(isinstance(s, dict) and s.get("is_processing") is True for s in data)
            except ValueError:
                busy = None
        if busy is None:                       # error or unparseable: wait out the grace
            if not sp.slots_unavailable_logged:
                sp.slots_unavailable_logged = True
                d.log.decision("slots_unavailable", job=jid)
            return
        if not busy:
            self.stop_pending.pop(jid, None)
            d.supervisor.stop(jid, sp.reason)


class _ProbeKey:
    """Selector payload for probes (the daemon dispatches by `.kind`)."""
    kind = "probe"

    def __init__(self, probe: Probe):
        self.obj = probe


def _shutdown(self) -> None:
    for pr in list(self.probes.values()):
        try:
            self.d.sel.unregister(pr.sock)
        except (KeyError, ValueError):
            pass
        pr.sock.close()
    self.probes.clear()


Residents.shutdown = _shutdown
