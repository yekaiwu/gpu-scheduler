"""Yield apps (QUESTIONS #41): desktop apps the owner lets the daemon quit while their memory is needed, and reopen after.

The daemon never signals a process it did not start, with this one opt-in exception: the owner names an app bundle
in `yield_apps` and says when it may be closed:

* `reservations`: while one of these reservation windows is active (closed once per window, at its start);
* `for_jobs_gib`: when a queued job reserving at least this many GiB has been blocked ONLY by live memory for
  `job_wait_s`, and closing the app would free enough for it.

Safety rules:

* `busy_ports`: never quit while another process has an ESTABLISHED TCP connection to one of these ports (a data
  client talking to the app's local API). The daemon retries every 30 s while the need lasts.
* Quit gracefully only: AppleScript `quit` (default), or one SIGTERM to the app's top-level main process (`sigterm`;
  graceful for Electron/Chromium apps, NOT for plain Cocoa apps). Never SIGKILL. If the app is still running after
  `quit_timeout_s` (a save dialog, a hung app), the daemon gives up for this episode and leaves it alone.
* Reopen only what the daemon itself closed, `reopen_delay_s` after the last need ended, with `open -g` (no focus
  steal). If the owner reopens the app during an episode, that episode never closes it again.
* Every helper (lsof, osascript, open) is a non-blocking subprocess polled by the tick and SIGKILLed on timeout,
  so a TCC Automation prompt nobody answers cannot freeze the daemon.

State survives a daemon restart in the meta table (`yield_app:<name>`): an app the daemon closed is still reopened
once its need is over.
"""

from __future__ import annotations

import json
import os
import plistlib
from dataclasses import dataclass, field

from gsched.platform import JobIdentity
from gsched.policy import reservations
from gsched.policy.fit import live_free_bytes
from gsched.units import GiB, gib_to_bytes

META_PREFIX = "yield_app:"
LSOF = "/usr/sbin/lsof"
OSASCRIPT = "/usr/bin/osascript"
OPEN = "/usr/bin/open"
HELPER_TIMEOUT_S = 30.0
BUSY_RETRY_S = 30.0
FIT_FRACTION = 0.8          # of the app's footprint counted as freed when deciding whether closing it helps a job
SIGTERM = 15

IDLE, BUSY_CHECK, QUITTING, CLOSED, REOPENING = "idle", "busy_check", "quitting", "closed", "reopening"


@dataclass
class AppSpec:
    name: str
    app: str
    reservations: tuple
    for_jobs_bytes: int | None
    job_wait_s: int
    busy_ports: tuple
    quit: str
    quit_timeout_s: int
    reopen: bool
    reopen_delay_s: int

    @classmethod
    def from_cfg(cls, a: dict) -> "AppSpec":
        fj = a.get("for_jobs_gib")
        return cls(name=a["name"], app=os.path.normpath(os.path.expanduser(a["app"])),
                   reservations=tuple(a.get("reservations", ())),
                   for_jobs_bytes=None if fj is None else gib_to_bytes(fj), job_wait_s=int(a.get("job_wait_s", 60)),
                   busy_ports=tuple(a.get("busy_ports", ())), quit=a.get("quit", "applescript"),
                   quit_timeout_s=int(a.get("quit_timeout_s", 60)), reopen=bool(a.get("reopen", True)),
                   reopen_delay_s=int(a.get("reopen_delay_s", 60)))


@dataclass
class AppState:
    phase: str = IDLE
    reason: str | None = None          # human text: "reservation t233-pass", "job <id> (24 GiB)"
    episode: str | None = None         # the need this phase serves: "r:<name>:<date>" or "j:<job id>"
    since_ms: int | None = None        # when the daemon closed it
    freed_bytes: int = 0
    skip: set = field(default_factory=set)      # episodes that must not close the app again
    helper: object = None
    helper_kind: str | None = None
    deadline_mono: float = 0.0
    retry_mono: float = 0.0
    no_need_since_ms: int | None = None
    last_busy_log_mono: float = -1e9
    blocked_since: dict = field(default_factory=dict)   # job id -> first ms seen blocked only by memory_live
    app: str | None = None             # bundle path as persisted (to reopen an app whose entry left the config)


def _bundle_info(app: str) -> tuple[str, str | None]:
    """(main executable path, bundle id). Info.plist is read once; /Applications is not a TCC-protected root."""
    exe_name, bid = os.path.basename(app)[:-4], None
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
            info = plistlib.load(fh)
        exe_name = info.get("CFBundleExecutable") or exe_name
        bid = info.get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    return os.path.join(app, "Contents", "MacOS", exe_name), bid


class YieldApps:
    def __init__(self, daemon):
        self.d = daemon
        self.state: dict[str, AppState] = {}
        self._bundles: dict[str, tuple] = {}
        self._loaded = False
        self._orphans: list = []

    # ---- config / persistence ----------------------------------------------------------------------------------
    def specs(self) -> list[AppSpec]:
        return [AppSpec.from_cfg(a) for a in self.d.cfg.yield_apps]

    def _bundle(self, sp: AppSpec) -> tuple:
        if sp.app not in self._bundles:
            self._bundles[sp.app] = _bundle_info(sp.app)
        return self._bundles[sp.app]

    def _load(self) -> None:
        self._loaded = True
        for k, v in self.d.store.meta_prefix(META_PREFIX).items():
            try:
                m = json.loads(v)
            except ValueError:
                continue
            st = self.state.setdefault(k[len(META_PREFIX):], AppState())
            st.skip = set(m.get("skip", []))
            if m.get("phase") in (CLOSED, QUITTING):
                st.phase, st.reason, st.episode = CLOSED, m.get("reason"), m.get("episode")
                st.since_ms, st.freed_bytes = m.get("since_ms"), int(m.get("freed_bytes", 0))
                st.app = m.get("app")

    def _save(self, sp: AppSpec) -> None:
        st = self.state[sp.name]
        st.app = sp.app
        body = {"phase": st.phase if st.phase in (CLOSED, QUITTING) else IDLE, "app": sp.app, "reason": st.reason,
                "episode": st.episode, "since_ms": st.since_ms, "freed_bytes": st.freed_bytes,
                "skip": sorted(st.skip)[-20:]}
        self.d.store.meta_set(META_PREFIX + sp.name, json.dumps(body))

    # ---- processes -----------------------------------------------------------------------------------------------
    def app_procs(self, sp: AppSpec) -> list:
        procs = getattr(self.d, "last_procs", None) or []
        root = sp.app.rstrip("/") + "/"
        return [p for p in procs if p.path and p.path.startswith(root)]

    def _mains(self, sp: AppSpec, procs: list) -> list:
        """Top-level instances of the bundle's main executable (a re-exec'd child of the main is not top-level)."""
        exe, _ = self._bundle(sp)
        mains = [p for p in procs if p.path == exe]
        pids = {p.pid for p in mains}
        return [p for p in mains if p.ppid not in pids]

    # ---- need ------------------------------------------------------------------------------------------------------
    def need(self, sp: AppSpec, st: AppState, snap, app_bytes: int) -> tuple[str, str] | None:
        """(episode, reason) of the current need, or None. Pure apart from the blocked-since bookkeeping."""
        cfg, now = self.d.cfg, snap.now_ms
        for r in reservations.parse(cfg):
            if r.name in sp.reservations:
                occ = reservations.current(r, now, cfg)
                if occ is not None:
                    return f"r:{r.name}:{occ.date}", f"reservation {r.name}"
        if sp.for_jobs_bytes is None:
            st.blocked_since.clear()
            return None
        jobs = {j.id: j for j in snap.jobs}
        # Keep serving a job episode while its job is queued or running, and while any heavy job admitted after the
        # app was closed is still running (it was admitted on the strength of the freed memory).
        if st.phase == CLOSED and st.episode and st.episode.startswith("j:"):
            j = jobs.get(st.episode[2:])
            if j is not None and j.state in ("queued", "held"):
                return st.episode, st.reason or f"job {j.id}"
            for x in snap.jobs:
                if (x.state == "held" and x.memory_bytes >= sp.for_jobs_bytes and st.since_ms is not None
                        and (x.started_ms or x.held_ms or 0) >= st.since_ms):
                    return st.episode, f"job {x.id} ({x.memory_bytes / GiB:.0f} GiB) admitted while it was closed"
        blocked = self.d.last_blocked or {}
        live = live_free_bytes(snap, cfg)
        seen = set()
        best = None
        for j in snap.jobs:
            if j.state != "queued" or j.memory_bytes < sp.for_jobs_bytes or blocked.get(j.id) != ["memory_live"]:
                continue
            seen.add(j.id)
            first = st.blocked_since.setdefault(j.id, now)
            if now - first < sp.job_wait_s * 1000:
                continue
            if live + int(FIT_FRACTION * app_bytes) < j.memory_bytes:
                continue                 # closing it would not be enough: do not close it for nothing
            if best is None:
                best = j
        for k in list(st.blocked_since):
            if k not in seen:
                del st.blocked_since[k]
        if best is not None:
            return f"j:{best.id}", f"job {best.id} ({best.memory_bytes / GiB:.0f} GiB) blocked by live memory"
        return None

    # ---- the tick ------------------------------------------------------------------------------------------------
    def tick(self, snap) -> None:
        d = self.d
        if not self._loaded:
            self._load()
        specs = {sp.name: sp for sp in self.specs()}
        mono = d.clock.mono()
        for h in list(self._orphans):
            if h.poll() is not None:
                h.close()
                self._orphans.remove(h)
        for name in list(self.state):
            if name not in specs:                       # removed from config: reopen what we closed, forget it
                st = self.state.pop(name)
                if st.helper is not None:
                    d.platform.kill_async(st.helper)
                    self._close_helper(st)
                if st.phase == CLOSED and st.app:
                    d.log.decision("yield_app_unconfigured", app=name, reopen=st.app)
                    self._orphans.append(d.platform.spawn_async([OPEN, "-g", "-a", st.app]))
                d.store.meta_delete(META_PREFIX + name)
        for name, sp in specs.items():
            st = self.state.setdefault(name, AppState())
            procs = self.app_procs(sp)
            mains = self._mains(sp, procs)
            app_bytes = sum(p.footprint_bytes or 0 for p in procs)
            want = self.need(sp, st, snap, app_bytes) if d.cfg.enforce else None
            self._step(sp, st, snap.now_ms, mono, mains, app_bytes, want)

    def _step(self, sp: AppSpec, st: AppState, now: int, mono: float, mains: list, app_bytes: int, want) -> None:
        d = self.d
        log = d.log.decision
        if st.phase == IDLE:
            if want is None or want[0] in st.skip or mono < st.retry_mono or not mains:
                return
            st.episode, st.reason = want
            if sp.busy_ports:
                argv = [LSOF, "-nP"] + [f"-iTCP:{p}" for p in sp.busy_ports] + ["-sTCP:ESTABLISHED", "-t"]
                self._spawn(st, "lsof", argv, mono + HELPER_TIMEOUT_S)
                st.phase = BUSY_CHECK
                return
            self._quit(sp, st, now, mono, mains, app_bytes)
            return
        if st.phase == BUSY_CHECK:
            res = self._poll(st, mono)
            if res is None:
                return
            rc, out = res
            if rc == "timeout":
                busy_pids = ["?"]
            else:
                own = {p.pid for p in self.app_procs(sp)} | {os.getpid()}
                busy_pids = [int(x) for x in out.split() if x.isdigit() and int(x) not in own]
            if busy_pids:
                if mono - st.last_busy_log_mono >= 600:
                    st.last_busy_log_mono = mono
                    log("yield_app_busy", app=sp.name, reason=st.reason, clients=busy_pids[:10],
                        ports=list(sp.busy_ports))
                st.phase, st.retry_mono = IDLE, mono + BUSY_RETRY_S
                return
            if want is None or want[0] != st.episode or not mains:
                st.phase = IDLE
                return
            self._quit(sp, st, now, mono, mains, app_bytes)
            return
        if st.phase == QUITTING:
            if st.helper is not None:
                res = self._poll(st, mono)          # osascript: its exit code does not decide anything
                if res is not None and res[0] not in (0, "timeout"):
                    log("yield_app_quit_refused", app=sp.name, rc=res[0], output=str(res[1])[-300:])
            if not mains:
                st.phase, st.since_ms = CLOSED, now
                st.no_need_since_ms = None
                self._save(sp)
                log("yield_app_closed", app=sp.name, reason=st.reason, freed_gib=round(st.freed_bytes / GiB, 2))
                return
            if mono >= st.deadline_mono:
                if st.helper is not None:
                    d.platform.kill_async(st.helper)
                    self._close_helper(st)
                log("yield_app_quit_failed", app=sp.name, reason=st.reason, after_s=sp.quit_timeout_s,
                    hint="still running (a save or confirm dialog?); left alone for this episode")
                st.skip.add(st.episode)
                st.phase = IDLE
                self._save(sp)
            return
        if st.phase == CLOSED:
            if mains:                                  # the owner (or something else) opened it again
                log("yield_app_reopened_by_user", app=sp.name, episode=st.episode)
                if want is not None:
                    st.skip.add(want[0])
                st.skip.add(st.episode)
                st.phase, st.since_ms = IDLE, None
                self._save(sp)
                return
            if want is not None:
                if want[0] != st.episode:
                    log("yield_app_kept_closed", app=sp.name, was=st.reason, now=want[1])
                    st.episode, st.reason = want
                    self._save(sp)
                st.no_need_since_ms = None
                return
            if st.no_need_since_ms is None:
                st.no_need_since_ms = now
            if now - st.no_need_since_ms < sp.reopen_delay_s * 1000:
                return
            if not sp.reopen:
                log("yield_app_released", app=sp.name, reason=st.reason, reopen=False)
                st.phase, st.since_ms = IDLE, None
                self._save(sp)
                return
            self._spawn(st, "open", [OPEN, "-g", "-a", sp.app], mono + HELPER_TIMEOUT_S)
            st.phase = REOPENING
            return
        if st.phase == REOPENING:
            res = self._poll(st, mono)
            if res is None:
                return
            closed_s = None if st.since_ms is None else round((now - st.since_ms) / 1000)
            if res[0] == 0:
                log("yield_app_reopened", app=sp.name, reason=st.reason, closed_s=closed_s)
            else:
                log("yield_app_reopen_failed", app=sp.name, rc=res[0], output=str(res[1])[-300:], closed_s=closed_s)
            st.phase, st.since_ms, st.no_need_since_ms = IDLE, None, None
            if st.episode:
                st.skip.add(st.episode)              # a still-active need (job episode just ended) never re-closes it
            self._save(sp)

    # ---- actions -------------------------------------------------------------------------------------------------
    def _quit(self, sp: AppSpec, st: AppState, now: int, mono: float, mains: list, app_bytes: int) -> None:
        d = self.d
        st.freed_bytes = app_bytes
        d.log.decision("yield_app_quit", app=sp.name, reason=st.reason, method=sp.quit,
                       app_gib=round(app_bytes / GiB, 2), pids=[p.pid for p in mains])
        if sp.quit == "sigterm":
            for p in mains:
                d.platform.signal_pid(JobIdentity(p.pid, p.start_sec, p.start_usec, p.path, p.pgid), SIGTERM)
        else:
            _, bid = self._bundle(sp)
            target = f'application id "{bid}"' if bid else f'application "{sp.app}"'
            self._spawn(st, "osascript", [OSASCRIPT, "-e", f"tell {target} to quit"], mono + sp.quit_timeout_s)
        st.phase, st.deadline_mono = QUITTING, mono + sp.quit_timeout_s
        st.since_ms = now
        self._save(sp)

    def _spawn(self, st: AppState, kind: str, argv: list, deadline: float) -> None:
        st.helper = self.d.platform.spawn_async(argv)
        st.helper_kind, st.deadline_mono = kind, deadline

    def _poll(self, st: AppState, mono: float):
        """(rc, output) once the helper finished; ("timeout", "") after SIGKILLing a helper past its deadline."""
        if st.helper is None:
            return None
        res = st.helper.poll()
        if res is None:
            if mono < st.deadline_mono:
                return None
            self.d.platform.kill_async(st.helper)
            res = ("timeout", "")
        self._close_helper(st)
        return res

    def _close_helper(self, st: AppState) -> None:
        try:
            st.helper.close()
        except Exception:
            pass
        st.helper, st.helper_kind = None, None

    def shutdown(self) -> None:
        for st in self.state.values():
            if st.helper is not None:
                try:
                    self.d.platform.kill_async(st.helper)
                except Exception:
                    pass
                self._close_helper(st)

    # ---- status ---------------------------------------------------------------------------------------------------
    def status(self) -> list:
        out = []
        for sp in self.specs():
            st = self.state.get(sp.name, AppState())
            procs = self.app_procs(sp)
            out.append({"name": sp.name, "app": sp.app, "phase": st.phase, "reason": st.reason,
                        "since_ms": st.since_ms, "running": bool(self._mains(sp, procs)),
                        "app_gib": round(sum(p.footprint_bytes or 0 for p in procs) / GiB, 2),
                        "freed_gib": round(st.freed_bytes / GiB, 2) if st.phase == CLOSED else None,
                        "reservations": list(sp.reservations),
                        "for_jobs_gib": None if sp.for_jobs_bytes is None else round(sp.for_jobs_bytes / GiB, 2)})
        return out
