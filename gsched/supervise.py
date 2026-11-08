"""Daemon-side supervision: spawn through the shim, the release gate, exit records, reconciliation (07)."""

from __future__ import annotations

import json
import os
import signal as _signal
import sys
import types
from dataclasses import dataclass

from gsched.platform import JobIdentity
from gsched.spec import current_user, job_env

SIG_TERM, SIG_KILL = int(_signal.SIGTERM), int(_signal.SIGKILL)
# stop reasons whose exit puts a restartable job back in the queue
REQUEUE_REASONS = {"preempt", "morning", "pressure", "protective", "switch", "reservation"}
# stop reasons that end a resident (it is re-created by `ensure`)
END_REASONS = {"idle": "idle_unload", "drain": "drain_unload"}
MAX_SPAWN_FAILURES = 3


@dataclass
class PendingSpawn:
    job_id: str
    gate_w: int
    rep_r: int
    deadline_mono: float


@dataclass
class CwdWait:
    """After the gate opened: the workload chdir()s (it may block behind a TCC consent prompt) and then writes
    `cwd_ok` to the report pipe. The daemon only ever waits for that line through the selector."""
    job_id: str
    rep_r: int
    deadline_mono: float


def sig_name(n: int) -> str:
    try:
        return _signal.Signals(n).name
    except ValueError:
        return str(n)


def ready_valid(checkpoint_dir: str, stop_requested_ms: int | None) -> bool:
    """READY exists, starts with 'ready\\n', and is newer than the checkpoint request (08 section 1)."""
    p = os.path.join(checkpoint_dir, "READY")
    try:
        with open(p, "rb") as fh:
            head = fh.read(6)
        if head != b"ready\n":
            return False
        if stop_requested_ms is None:
            return True
        return os.stat(p).st_mtime_ns >= stop_requested_ms * 1_000_000
    except OSError:
        return False


class Supervisor:
    def __init__(self, daemon):
        self.d = daemon
        self.pending: dict[str, PendingSpawn] = {}
        self.cwd_wait: dict[str, CwdWait] = {}
        self._ext_gone: dict[str, float] = {}      # external job -> mono when its pid was first seen gone
        self.aborting: dict[str, float] = {}      # job id -> mono of abort (shim kill after 5 s)
        self.caff: dict[str, int] = {}
        self._path_ok: set = set()
        self._spawn_failures: dict[str, int] = {}

    # ---- identities ------------------------------------------------------------------------
    def workload_ident(self, row, *, use_path: bool | None = None) -> JobIdentity | None:
        if row["pid"] is None or row["pid_start_sec"] is None:
            return None
        if use_path is None:
            use_path = row["spawn"] == "daemon" and (row["id"], row["attempt"]) in self._path_ok
        return JobIdentity(row["pid"], row["pid_start_sec"], row["pid_start_usec"],
                           row["pid_path"] if use_path else None, row["pgid"] or row["pid"])

    def shim_ident(self, row) -> JobIdentity | None:
        if row["shim_pid"] is None or row["shim_start_sec"] is None:
            return None
        return JobIdentity(row["shim_pid"], row["shim_start_sec"], row["shim_start_usec"], None, row["shim_pid"])

    def _job_dir(self, job_id: str) -> str:
        return self.d.paths.job_dir(job_id)

    def exit_file(self, job_id: str) -> str:
        return os.path.join(self._job_dir(job_id), "exit.json")

    # ---- spawn ---------------------------------------------------------------------------------
    def spawn(self, job_id: str) -> None:
        d = self.d
        row = d.store.get(job_id)
        if row is None or row["state"] != "held" or row["held_reason"] != "spawning":
            return
        now = d.clock.now_ms()
        spec_ns = types.SimpleNamespace(env=json.loads(row["env_json"]), project=row["project"],
                                        memory_bytes=row["memory_bytes"])
        env = job_env(spec_ns, job_id, row["attempt"], bool(row["resume"]), row["checkpoint_dir"], d.cfg,
                      current_user())
        jd = self._job_dir(job_id)
        os.makedirs(jd, mode=0o700, exist_ok=True)
        try:
            os.unlink(self.exit_file(job_id))     # a stale record from an earlier attempt must not be read
        except FileNotFoundError:
            pass
        stdout = row["stdout_path"] or os.path.join(jd, "stdout.log")
        stderr = row["stderr_path"] or os.path.join(jd, "stderr.log")
        command = json.loads(row["command_json"])
        gate_r, gate_w = os.pipe()
        rep_r, rep_w = os.pipe()
        os.set_inheritable(gate_r, True)
        os.set_inheritable(rep_w, True)
        argv = [d.shim_python, "-I", "-m", "gsched.shim", "--job", job_id, "--gate-fd", str(gate_r),
                "--report-fd", str(rep_w), "--exit-file", self.exit_file(job_id)]
        if row["cwd"]:
            argv += ["--cwd", row["cwd"]]       # the workload chdir()s; Popen(cwd=...) would block THIS thread (TCC)
        argv += ["--"] + command
        try:
            shim_pid = d.platform.spawn_shim(argv, env=env, cwd=jd, stdout_path=stdout, stderr_path=stderr,
                                             pass_fds=(gate_r, rep_w))
        except OSError as e:
            for fd in (gate_r, gate_w, rep_r, rep_w):
                os.close(fd)
            d.store.finish(job_id, now, state="failed", end_reason="spawn_error")
            d.log.decision("spawn_error", job=job_id, error=str(e))
            d.admit_hint = True
            d.notify_parked(job_id)
            return
        os.close(gate_r)
        os.close(rep_w)
        ident = d.platform.proc(shim_pid)
        if ident is None:
            d.store.record_shim(job_id, shim_pid, 0, 0)
            self.pending[job_id] = PendingSpawn(job_id, gate_w, rep_r, d.clock.mono())
            self.abort_spawn(job_id, "shim_vanished")
            return
        d.store.record_shim(job_id, shim_pid, ident.start_sec, ident.start_usec)
        os.set_blocking(rep_r, False)
        self.pending[job_id] = PendingSpawn(job_id, gate_w, rep_r,
                                            d.clock.mono() + d.cfg.spawn_report_timeout_ms / 1000)
        d.register_report(rep_r, job_id)
        d.log.decision("spawn", job=job_id, shim_pid=shim_pid, argv0=os.path.basename(command[0]),
                       argc=len(command) - 1)

    def on_report_readable(self, job_id: str) -> None:
        d = self.d
        cw = self.cwd_wait.get(job_id)
        if cw is not None:
            self._on_cwd_readable(cw)
            return
        ps = self.pending.get(job_id)
        if ps is None:
            return
        try:
            data = os.read(ps.rep_r, 64)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data.strip():
            self.abort_spawn(job_id, "report_eof")
            return
        row = d.store.get(job_id)
        try:
            pid = int(data.strip().split()[0])
        except ValueError:
            self.abort_spawn(job_id, "bad_report")
            return
        if row is None or row["state"] != "held" or row["held_reason"] != "spawning":
            self.abort_spawn(job_id, "state_changed")
            return
        info = d.platform.proc(pid)
        shim_pid = row["shim_pid"]
        if info is None or info.ppid != shim_pid or info.pgid != shim_pid or info.uid != os.getuid():
            self.abort_spawn(job_id, "bad_report")
            return
        now = d.clock.now_ms()
        ident = JobIdentity(pid, info.start_sec, info.start_usec, info.path, info.pgid)
        ready = not d.residents.health_applies(row)
        if not d.store.begin_child(job_id, ident, now, ready=ready):
            self.abort_spawn(job_id, "begin_child_failed")
            return
        # COMMITTED. Only now may the workload exec.
        try:
            os.write(ps.gate_w, b"1")
        except OSError:
            self.abort_spawn(job_id, "gate_write_failed")
            return
        self._close_pending(job_id, keep_report=True)
        self.cwd_wait[job_id] = CwdWait(job_id, ps.rep_r, d.clock.mono() + d.cfg.spawn_report_timeout_ms / 1000)
        d.store.mark_gate_released(job_id, now, ready=ready)
        d.log.decision("gate_released", job=job_id, pid=pid, path=info.path)
        if d.cfg.prevent_sleep:
            cp = d.platform.caffeinate(shim_pid)
            if cp:
                self.caff[job_id] = cp
        d.notify_parked(job_id)

    def _close_pending(self, job_id: str, keep_report: bool = False) -> PendingSpawn | None:
        ps = self.pending.pop(job_id, None)
        if ps is None:
            return None
        fds = (ps.gate_w,) if keep_report else (ps.gate_w, ps.rep_r)
        if not keep_report:
            self.d.unregister_report(ps.rep_r)
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        return ps

    def _close_cwd_wait(self, job_id: str) -> None:
        cw = self.cwd_wait.pop(job_id, None)
        if cw is None:
            return
        self.d.unregister_report(cw.rep_r)
        try:
            os.close(cw.rep_r)
        except OSError:
            pass

    def _on_cwd_readable(self, cw: CwdWait) -> None:
        try:
            data = os.read(cw.rep_r, 64)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        # "cwd_ok" arrives once; EOF without it means the workload exited (e.g. chdir failed: exit 126 + a stderr line).
        # Either way the wait is over; the shim's exit record does the rest.
        self._close_cwd_wait(cw.job_id)

    def abort_spawn(self, job_id: str, reason: str) -> None:
        """Close the gate WITHOUT writing: the workload reads EOF and exits 111 without exec'ing."""
        if self._close_pending(job_id) is None and job_id not in self.pending:
            pass
        self.aborting[job_id] = self.d.clock.mono()
        self.d.log.decision("spawn_abort", job=job_id, reason=reason)

    def expire_spawn_and_attach(self, now_ms: int) -> None:
        d = self.d
        mono = d.clock.mono()
        for job_id, ps in list(self.pending.items()):
            if mono >= ps.deadline_mono:
                self.abort_spawn(job_id, "report_timeout")
        for job_id, cw in list(self.cwd_wait.items()):
            if mono >= cw.deadline_mono:
                self._cwd_blocked(job_id)
        for job_id, t in list(self.aborting.items()):
            row = d.store.get(job_id)
            if row is None or row["state"] != "held":
                self.aborting.pop(job_id, None)
                continue
            if mono - t >= 5.0:
                sid = self.shim_ident(row)
                # safe: nothing has exec'd (the gate never opened)
                if sid is not None and row["gate_released"] == 0:
                    d.platform.signal_group(sid, SIG_KILL)
                    d.log.decision("signal", job=job_id, pgid=sid.pgid, sig="SIGKILL", reason="spawn_abort")
                self.aborting.pop(job_id, None)
        # external launchers: lease issued, no attach yet
        for row in d.store.held_jobs():
            if row["spawn"] == "external" and row["held_reason"] == "await_attach" and row["pid"] is None \
                    and row["held_ms"] is not None and now_ms - row["held_ms"] >= d.cfg.attach_grace_ms:
                d.store.finish(row["id"], now_ms, state="cancelled", end_reason="attach_timeout")
                d.log.decision("exit", job=row["id"], state="cancelled", reason="attach_timeout")
                d.admit_hint = True
                d.notify_parked(row["id"])

    def _cwd_blocked(self, job_id: str) -> None:
        """The workload never reported `cwd_ok`: its chdir() is stuck (TCC consent prompt, a dead network mount).
        Nothing of the user's command has exec'd yet, so stopping it is safe whatever the checkpoint mode.
        It ends `failed` with end_reason=cwd_blocked."""
        d = self.d
        self._close_cwd_wait(job_id)
        row = d.store.get(job_id)
        if row is None or row["state"] != "held" or row["held_reason"] != "live":
            return
        d.log.decision("cwd_blocked", job=job_id, cwd=row["cwd"], timeout_ms=d.cfg.spawn_report_timeout_ms)
        self.stop(job_id, "cwd_blocked")

    # ---- signals (all through the platform) ----------------------------------------------------------
    def _escapees(self, row) -> list[JobIdentity]:
        """Tree members that left the job's process group (setsid). Never used for checkpoint requests."""
        scan = self.d.last_scan
        out = []
        if scan is None:
            return out
        for pid in scan.trees.get(row["id"], ()):
            p = scan.by_pid.get(pid)
            # The workload itself counts as an escapee once it has left the job's group (it called setsid/setpgid
            # itself): signal_group then refuses (pgid mismatch), and without this nothing would ever reach it.
            if p is None or p.pgid == row["pgid"] or pid == row["shim_pid"]:
                continue
            if pid == row["pid"] and (p.start_sec, p.start_usec) != (row["pid_start_sec"], row["pid_start_usec"]):
                continue
            out.append(JobIdentity(p.pid, p.start_sec, p.start_usec, p.path, p.pgid))
        return out

    def _signal_group(self, row, sig: int, reason: str) -> bool:
        ident = self.workload_ident(row)
        if ident is None:
            return False
        ok = self.d.platform.signal_group(ident, sig)
        self.d.log.decision("signal", job=row["id"], pgid=ident.pgid, sig=sig_name(sig), reason=reason, sent=ok)
        for esc in self._escapees(row):
            if self.d.platform.signal_pid(esc, sig):
                self.d.log.decision("signal", job=row["id"], pid=esc.pid, sig=sig_name(sig), reason=reason + "_escapee")
        return ok

    def stop(self, job_id: str, reason: str) -> bool:
        """begin_stop (commit), then SIGTERM to the group. Only daemon-spawned jobs are ever signaled."""
        d = self.d
        row = d.store.get(job_id)
        if row is None or row["state"] != "held" or row["spawn"] != "daemon":
            return False
        # Ground rule 2, enforced here as well as in the planners: only restartable jobs may be stopped on policy.
        # atomic/cooperative/spilled jobs are stopped only by an explicit `cancel --terminate`, or (cooperative)
        # by the job's own on_timeout=kill opt-in.
        allowed = {"restartable": None, "cooperative": {"terminate", "checkpoint_timeout_kill", "cwd_blocked"},
                   "atomic": {"terminate", "cwd_blocked"}}.get(row["checkpoint"], set())
        # cwd_blocked is not a policy stop: the workload is stuck in chdir() and has not exec'd the user's command.
        if allowed is not None and reason not in allowed:
            d.log.decision("stop_refused", job=job_id, reason=reason, checkpoint=row["checkpoint"])
            return False
        if not d.store.begin_stop(job_id, d.clock.now_ms(), reason):
            return False
        d.log.decision("stop", job=job_id, reason=reason)
        self._signal_group(d.store.get(job_id), SIG_TERM, reason)
        d.notify_parked(job_id)
        return True

    def escalate_kill(self, job_id: str) -> bool:
        """SIGKILL escalation, once, after kill_grace_s."""
        d = self.d
        row = d.store.get(job_id)
        if row is None or row["state"] != "held" or row["held_reason"] != "stopping" or row["kill_sent"]:
            return False
        d.store.set_flags(job_id, kill_sent=1)
        self._signal_group(row, SIG_KILL, row["stop_reason"] or "stop")
        return True

    def checkpoint(self, job_id: str, reason: str) -> bool:
        """Delete any stale READY, commit `checkpointing`, then one SIGUSR1 to the workload pid only."""
        d = self.d
        row = d.store.get(job_id)
        if row is None or row["state"] != "held" or row["spawn"] != "daemon" or row["held_reason"] != "live" \
                or row["checkpoint"] != "cooperative" or row["spilled"]:
            return False
        try:
            os.unlink(os.path.join(row["checkpoint_dir"], "READY"))
        except FileNotFoundError:
            pass
        if not d.store.request_checkpoint(job_id, d.clock.now_ms(), reason):
            return False
        d.log.decision("checkpoint_request", job=job_id, reason=reason, grace_s=row["grace_s"])
        ident = self.workload_ident(row)
        signo = int(getattr(_signal, d.cfg.checkpoint_signal))
        if ident is not None:
            ok = d.platform.signal_pid(ident, signo)
            d.log.decision("signal", job=job_id, pid=ident.pid, sig=sig_name(signo), reason=reason, sent=ok)
        d.notify_parked(job_id)
        return True

    # ---- reap and finalize (07 section 4) ---------------------------------------------------------------
    def _read_exit(self, row) -> dict | None:
        try:
            with open(self.exit_file(row["id"]), "rb") as fh:
                rec = json.loads(fh.read())
        except (OSError, ValueError):
            return None
        if not isinstance(rec, dict) or rec.get("job") != row["id"]:
            return None
        if "exit_code" not in rec and "signal" not in rec:
            return None
        if rec.get("attempt") is not None and rec["attempt"] != row["attempt"]:
            return None

        def _int_or_none(v, lo, hi):
            return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None
        # A record with wrong types would raise inside finalize on every tick and every restart (a crash loop under
        # launchd that stops all scheduling). Treat it like a missing record instead.
        code, sig = _int_or_none(rec.get("exit_code"), 0, 255), _int_or_none(rec.get("signal"), 1, 127)
        if code is None and sig is None:
            return None
        return dict(rec, exit_code=code, signal=sig,
                    peak_footprint_bytes=_int_or_none(rec.get("peak_footprint_bytes"), 1, 1 << 62))

    def reap_and_finalize(self, now_ms: int) -> None:
        d = self.d
        d.platform.reap()   # zombie cleanup only: exit detection is identity + exit.json, always
        for row in d.store.held_jobs():
            if row["spawn"] != "daemon":
                continue
            sid = self.shim_ident(row)
            if sid is not None and d.platform.identity_matches(sid):
                continue
            if sid is None and row["pid"] is not None:
                continue          # shim lost earlier; check_identities watches the workload
            rec = self._read_exit(row)
            if rec is None and row["gate_released"] and self._workload_alive(row):
                # The shim died without a record (kill -9, jetsam) but the workload it exec'd is still running:
                # keep the job (its memory is still in use), exactly as reconcile does after a restart (07 section 6).
                self._shim_lost(row)
                continue
            if row["id"] in self.pending:
                self._close_pending(row["id"])
            self.finalize(row, rec, now_ms)

    def _workload_alive(self, row) -> bool:
        ident = self.workload_ident(row, use_path=False)
        return ident is not None and self.d.platform.identity_matches(ident)

    def _shim_lost(self, row) -> None:
        d = self.d
        d.store.clear_shim(row["id"])
        d.log.decision("shim_lost", job=row["id"], pid=row["pid"])
        self.caff.pop(row["id"], None)          # its caffeinate -w <shim> ended with the shim
        if d.cfg.prevent_sleep and row["pid"] is not None:
            cp = d.platform.caffeinate(row["pid"])
            if cp:
                self.caff[row["id"]] = cp

    def finalize(self, row, rec: dict | None, now_ms: int) -> None:
        d = self.d
        st = d.store
        jid = row["id"]
        code, sig = (rec.get("exit_code"), rec.get("signal")) if rec else (None, None)
        peak = rec.get("peak_footprint_bytes") if rec else None
        reason = row["stop_reason"]
        out_state = None
        if rec is None:
            if row["held_reason"] == "stopping" and row["kill_sent"]:
                code, sig = None, SIG_KILL         # we SIGKILLed the whole group, including the shim
            elif row["gate_released"] == 0:
                self._unstarted(row, now_ms)
                self._cleanup(row)
                return
            else:
                st.mark_lost(jid, now_ms, "exit_unknown")
                d.log.decision("lost", job=jid, why="exit_unknown")
                self._cleanup(row)
                return
        if code == 111 and row["gate_released"] == 0:
            self._unstarted(row, now_ms)
        elif row["held_reason"] == "checkpointing" and code == 0 and ready_valid(row["checkpoint_dir"],
                                                                                  row["stop_requested_ms"]):
            if row["cancel_requested"]:
                out_state = st.finish(jid, now_ms, exit_code=code, peak_footprint_bytes=peak)
            else:
                queue, _ = d.assign_queue_for_row(row)
                st.requeue_continuation(jid, now_ms, queue)
                d.log.decision("checkpoint_ok", job=jid, attempt=row["attempt"] + 1)
                d.log.decision("requeue", job=jid, attempt=row["attempt"] + 1, reason="checkpoint")
        elif row["held_reason"] == "stopping" and not row["cancel_requested"] and row["checkpoint"] == "restartable" \
                and reason in REQUEUE_REASONS:
            if peak:
                st.set_flags(jid, peak_footprint_bytes=max(peak, row["peak_footprint_bytes"] or 0))
            st.requeue_restartable(jid, now_ms, to_state="queued")
            d.log.decision("requeue", job=jid, attempt=row["attempt"] + 1, reason=reason)
        elif row["held_reason"] == "stopping" and not row["cancel_requested"] and reason in END_REASONS:
            if peak:
                st.set_flags(jid, peak_footprint_bytes=max(peak, row["peak_footprint_bytes"] or 0))
            st.requeue_restartable(jid, now_ms, to_state="done", exit_code=code, exit_signal=sig,
                                   end_reason=END_REASONS[reason])
            d.log.decision("exit", job=jid, state="done", reason=END_REASONS[reason])
        else:
            er = None
            if row["held_reason"] == "stopping" and reason in ("limit", "ceiling", "abort", "footprint_unreadable",
                                                                "checkpoint_timeout_kill", "cwd_blocked") \
                    and not row["cancel_requested"]:
                er = reason
            out_state = st.finish(jid, now_ms, exit_code=code, exit_signal=sig, peak_footprint_bytes=peak,
                                  end_reason=er if code != 0 or sig else None)
        if out_state:
            fin = st.get(jid)
            runtime = (now_ms - fin["started_ms"]) / 1000 if fin["started_ms"] else None
            d.log.decision("exit", job=jid, state=out_state, code=code, sig=sig, runtime_s=runtime,
                           peak_footprint_bytes=fin["peak_footprint_bytes"])
        self._cleanup(row)
        d.admit_hint = True
        d.notify_parked(jid)

    def _unstarted(self, row, now_ms: int) -> None:
        d = self.d
        jid = row["id"]
        if row["cancel_requested"]:
            d.store.finish(jid, now_ms, state="cancelled", end_reason="cancelled_before_start")
            d.log.decision("exit", job=jid, state="cancelled", reason="cancelled_before_start")
            return
        n = self._spawn_failures.get(jid, 0) + 1
        self._spawn_failures[jid] = n
        if n >= MAX_SPAWN_FAILURES:
            d.store.finish(jid, now_ms, state="failed", end_reason="spawn_error")
            d.log.decision("spawn_error", job=jid, error="spawn aborted repeatedly before exec")
            return
        d.store.requeue_unstarted(jid, now_ms)
        d.log.decision("requeue", job=jid, attempt=row["attempt"], reason="unstarted")

    def _cleanup(self, row) -> None:
        jid = row["id"]
        self._close_cwd_wait(jid)
        self.aborting.pop(jid, None)
        self.caff.pop(jid, None)
        self._path_ok.discard((jid, row["attempt"]))
        src = self.exit_file(jid)
        if os.path.exists(src):
            try:
                os.replace(src, os.path.join(self._job_dir(jid), f"exit.{row['attempt']}.json"))
            except OSError:
                pass

    # ---- identity checks (07 section 5) --------------------------------------------------------------------
    def check_identities(self, now_ms: int) -> None:
        d = self.d
        for row in d.store.held_jobs():
            if row["pid"] is None:
                continue
            jid = row["id"]
            # The one place the recorded path may change. pid + start time (sec AND usec) is what identifies the
            # process; exec keeps both and only replaces the executable. The first refresh after the gate release
            # records the command's binary (at report time the workload was still the shim's Python). A later exec
            # (a wrapper script ending in `exec llama-server`, `env`, `uv run`) is the same process too: refresh and
            # log it instead of calling the job lost. A different start time is a different process (never signaled).
            if row["spawn"] == "daemon" and row["gate_released"]:
                p = d.platform.proc(row["pid"])
                if p is not None and p.start_sec == row["pid_start_sec"] and p.start_usec == row["pid_start_usec"]:
                    first = (jid, row["attempt"]) not in self._path_ok
                    if p.path and p.path != row["pid_path"]:
                        d.store.refresh_pid_path(jid, p.path)
                        if not first:
                            d.log.decision("pid_path_changed", job=jid, pid=row["pid"], path=p.path)
                        row = d.store.get(jid)
                    self._path_ok.add((jid, row["attempt"]))
            ident = self.workload_ident(row, use_path=(row["spawn"] == "daemon" and
                                                       (jid, row["attempt"]) in self._path_ok))
            if ident is None or d.platform.identity_matches(ident):
                self._ext_gone.pop(jid, None)
                continue
            p = d.platform.proc(ident.pid)
            if row["spawn"] == "external":
                # A launcher (`gsched exec` especially) calls `finish` a few milliseconds after its child exits. If this
                # tick lands in that window the job would be marked lost although a clean exit report is on its way,
                # so require the pid to be seen gone on two ticks (>= 0.5 s apart) before calling it lost.
                first = self._ext_gone.setdefault(jid, d.clock.mono())
                if d.clock.mono() - first < 0.5:
                    continue
                self._ext_gone.pop(jid, None)
                why = "identity_mismatch" if p is not None else "exit_unknown"
                if p is None and d.store.meta_get(f"in_place:{jid}") == "1":
                    d.store.finish(jid, now_ms, state="done", end_reason="exit_unobserved")
                    d.store.meta_delete(f"in_place:{jid}")
                    d.log.decision("exit", job=jid, state="done", end_reason="exit_unobserved")
                    d.admit_hint = True
                    d.notify_parked(jid)
                    continue
                d.store.meta_delete(f"in_place:{jid}")
                d.store.mark_lost(jid, now_ms, why)
                d.log.decision("lost", job=jid, why=why)
                d.admit_hint = True
                d.notify_parked(jid)
                continue
            if p is not None:       # the pid now belongs to something else: never signal, mark lost
                d.store.mark_lost(jid, now_ms, "identity_mismatch")
                d.log.decision("lost", job=jid, why="identity_mismatch")
                self._cleanup(row)
                d.admit_hint = True
                d.notify_parked(jid)
                continue
            # workload gone: the shim reports shortly. If there is no shim at all, nothing will ever report.
            if row["shim_pid"] is None:
                rec = self._read_exit(row)
                self.finalize(row, rec, now_ms)

    # ---- reconcile at startup (07 section 6) --------------------------------------------------------------------
    def reconcile(self) -> dict:
        d = self.d
        now = d.clock.now_ms()
        summary = {"kept": 0, "finalized": 0, "lost": 0, "cancelled": 0, "requeued": 0}
        for row in d.store.held_jobs():
            jid = row["id"]
            if row["spawn"] == "external":
                if row["pid"] is None:
                    d.store.finish(jid, now, state="cancelled", end_reason="daemon_restart_before_attach")
                    summary["cancelled"] += 1
                elif d.platform.identity_matches(self.workload_ident(row, use_path=False)):
                    summary["kept"] += 1
                elif d.store.meta_get(f"in_place:{jid}") == "1":
                    d.store.finish(jid, now, state="done", end_reason="exit_unobserved")
                    d.store.meta_delete(f"in_place:{jid}")
                    summary["finalized"] += 1
                else:
                    d.store.mark_lost(jid, now, "exit_unknown")
                    summary["lost"] += 1
                continue
            rec = self._read_exit(row)
            sid = self.shim_ident(row)
            if rec is not None:
                self.finalize(row, rec, now)
                summary["finalized"] += 1
            elif sid is not None and d.platform.identity_matches(sid):
                summary["kept"] += 1
                if d.cfg.prevent_sleep:
                    cp = d.platform.caffeinate(sid.pid)
                    if cp:
                        self.caff[jid] = cp
            elif row["pid"] is not None and self._workload_alive(row):
                self._shim_lost(row)
                summary["kept"] += 1
            elif row["gate_released"] == 0 and row["pid"] is None:
                # the workload's pid was never even reported, so the gate cannot have been written: safe to retry.
                # With a pid recorded, the old daemon may have written the gate byte and died before
                # mark_gate_released committed; the command may have run, so fall through to lost (never run twice).
                d.store.requeue_unstarted(jid, now)
                summary["requeued"] += 1
            else:
                d.store.mark_lost(jid, now, "exit_unknown")
                d.log.decision("lost", job=jid, why="exit_unknown")
                summary["lost"] += 1
        return summary

    def shutdown(self) -> None:
        """Close gate pipes WITHOUT writing and without signaling anything (jobs keep running under their shims)."""
        for jid in list(self.pending):
            self._close_pending(jid)
        for jid in list(self.cwd_wait):
            self._close_cwd_wait(jid)

    def sleep_prevented(self) -> bool:
        return any(self.d.platform.pid_alive(p) for p in self.caff.values())
