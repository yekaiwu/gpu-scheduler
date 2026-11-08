"""The daemon: single-threaded event loop, connections, parking, lifecycle (05).

Exactly one thread. No threading, no asyncio. Every state change happens here, so two `submit`s can never race.
"""

from __future__ import annotations

import fcntl
import json
import os
import selectors
import signal
import socket
import sys
import time
import types
from dataclasses import dataclass, field

from gsched import __version__
from gsched.clock import Clock, SystemClock
from gsched.config import Config
from gsched.errors import ConfigError, GschedError, PlatformError
from gsched.log import DecisionLog
from gsched.paths import Paths, check_socket_path, mkdir_private
from gsched.policy.fit import compute_budget, compute_cpu_budget
from gsched.policy.pressure import SwapWindow
from gsched.projects import ProjectRegistry
from gsched.residents import Residents
from gsched.scan import ProtectiveState, Scanner
from gsched.spec import assign_queue
from gsched.supervise import Supervisor
from gsched.tcc import TccGuard
from gsched.store import Store
from gsched.tick import Ticker

RPC = "gpu-scheduler.rpc.v1"
MAX_REQUEST = 1024 * 1024
MAX_CONNS = 64
IDLE_TIMEOUT_S = 10.0
DOORS = ("cli", "mcp", "launcher", "dashboard")
EXIT_BUSY = 75


class DaemonBusy(Exception):
    """Another daemon holds the single-instance lock (exit 75)."""


@dataclass
class Parked:
    kind: str                 # wait_exit | wait_start | get_wait
    job: str
    deadline_mono: float | None
    last_seen: tuple
    door: str = "cli"


@dataclass
class Conn:
    sock: socket.socket
    uid: int
    peer_pid: int
    buf: bytearray = field(default_factory=bytearray)
    opened_mono: float = 0.0
    parked: Parked | None = None
    door: str = "cli"
    closed: bool = False


@dataclass
class Key:
    kind: str
    obj: object = None


def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


class Daemon:
    def __init__(self, home, cfg: Config | None = None, clock: Clock | None = None, platform=None, *,
                 log_enabled: bool = True, debug: bool | None = None, shim_python: str | None = None,
                 tcc_home: str | None = None):
        self.paths = Paths.resolve(home)
        self._cfg_given = cfg is not None
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.platform = platform
        self.log_enabled = log_enabled
        self.debug = bool(os.environ.get("GSCHED_DEBUG")) if debug is None else debug
        self.shim_python = shim_python or sys.executable
        self.tcc_home = tcc_home
        self.sel = selectors.DefaultSelector()
        self.listener: socket.socket | None = None
        self.conns: list[Conn] = []
        self.stopping = False
        self.admit_hint = False
        self.next_tick_mono = 0.0
        self.start_mono = 0.0
        self.store: Store | None = None
        self.log: DecisionLog | None = None
        self._lock_fh = None
        self._cfg_mtime: float | None = None
        self._cfg_unknown: set = set()
        self._wakeup_r = self._wakeup_w = None
        # in-memory state
        self.last_sample = None
        self.last_scan = None
        self.last_procs = None              # raw process table of the last good scan (yield_apps)
        self.last_blocked: dict = {}
        self.pressure = "normal"
        self.pressure_why: list = []
        self.swap_window = SwapWindow()
        self.swap_rate: float | None = None
        self.last_pressure_action_ms: int | None = None
        self.last_alarm_ms: int | None = None
        self.protective = ProtectiveState()
        self.offender_ids: dict = {}
        self.ceiling_noted: set = set()
        self.waiter_lost: dict[str, float] = {}
        self.reservation: str | None = None
        self.preempt_reservation: str | None = None
        self.draining = False
        self.reconcile_summary: dict = {}

    # ---- lifecycle ----------------------------------------------------------------------------------------
    def start(self, listen: bool = True) -> None:
        old_umask = os.umask(0o077)
        self._old_umask = old_umask
        self.paths.ensure_dirs()
        self.start_mono = self.clock.mono()
        if self.cfg is None:
            self.cfg = Config.load(self.paths.config, on_unknown=self._on_unknown_key, seen_unknown=self._cfg_unknown)
        self.cfg = self._effective(self.cfg)
        self._cfg_mtime = self._mtime(self.paths.config)
        if self.platform is None:
            from gsched.platform import get_platform
            self.platform = get_platform()
        self.machine = self.platform.machine()
        self.budget_bytes = compute_budget(self.machine, self.cfg)
        if self.budget_bytes <= 0:
            raise ConfigError("os_reserve_gib", "leaves no memory budget")
        self.cpu_budget = compute_cpu_budget(self.machine, self.cfg)
        self.store = Store(self.paths, self.clock)
        # single-instance lock
        self._lock_fh = open(self.paths.lockfile, "a")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_fh.close()
            self._lock_fh = None
            self.store.close()
            os.umask(self._old_umask)
            raise DaemonBusy("another gsched daemon is running (see run/daemon.pid)")
        try:
            self.log = DecisionLog(self.paths.log_file, self.cfg.log_max_bytes, self.clock, sink=self._event_sink,
                                   enabled=self.log_enabled)
            self.projects = ProjectRegistry(self.paths.projects, self._warn_cb)
            self.residents = Residents(self)
            self.supervisor = Supervisor(self)
            self.scanner = Scanner(self.platform, jobs_dir=self.paths.jobs)
            self.scanner.watch = (lambda fd, port: self.sel.register(fd, selectors.EVENT_READ, Key("lookup", port)),
                                  lambda fd: self.sel.unregister(fd))
            self.tcc = TccGuard(self, self.tcc_home)
            from gsched.yield_apps import YieldApps
            self.yield_apps = YieldApps(self)
            self.ticker = Ticker(self)
            from gsched.rpc import Handlers
            self.handlers = Handlers(self)
            now = self.clock.now_ms()
            prev = self.store.meta_get("memsize_bytes")
            if prev is not None and int(prev) != self.machine.memsize_bytes:
                self.log.decision("memsize_changed", old=int(prev), new=self.machine.memsize_bytes)
            self.store.meta_set("memsize_bytes", str(self.machine.memsize_bytes))
            self.store.meta_set("last_start_ms", str(now))
            self.draining = self.store.meta_get("drain", "0") == "1"
            self.last_pressure_action_ms = None
            self.sync_projects()
            self.residents.load_profiles()
            for o in self.store.offender_open():
                self.store.offender_clear(o["id"], now)
            self.reconcile_summary = self.supervisor.reconcile()
            self.tcc.start_all()      # non-blocking probe subprocesses (never a listdir in this process)
            self.store.retention(now, self.cfg.retention_days)
            if listen:
                self._listen()
            self._write_pidfile()
            self.log.decision("daemon_start", version=__version__, pid=os.getpid(), **self.reconcile_summary)
        except BaseException:
            self._abort_start()
            raise

    def _abort_start(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    def _listen(self) -> None:
        path = self.paths.socket(self.cfg.socket_path)
        check_socket_path(path)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            os.unlink(path)    # the lock proves no live daemon owns it
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        os.chmod(path, 0o600)
        s.listen(64)
        s.setblocking(False)
        self.listener = s
        self.socket_path = path
        self.sel.register(s, selectors.EVENT_READ, Key("listen"))

    def install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT stop the loop; SIGHUP reloads config. A self-pipe wakes select promptly."""
        self._wakeup_r, self._wakeup_w = os.pipe()
        os.set_blocking(self._wakeup_r, False)
        os.set_blocking(self._wakeup_w, False)
        signal.set_wakeup_fd(self._wakeup_w, warn_on_full_buffer=False)
        self.sel.register(self._wakeup_r, selectors.EVENT_READ, Key("wakeup"))

        def stop(signum, frame):
            self.stopping = True

        def hup(signum, frame):
            self._reload_requested = True

        self._reload_requested = False
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, hup)

    def _drain_wakeup(self) -> None:
        try:
            os.read(self._wakeup_r, 4096)
        except OSError:
            pass
        if getattr(self, "_reload_requested", False):
            self._reload_requested = False
            self.reload_all()

    def stop(self) -> None:
        """Clean shutdown. Does NOT signal any job: shims keep running and the next start reconciles."""
        if self.store is None:
            return
        try:
            self.log.decision("daemon_stop", version=__version__, pid=os.getpid())
        except Exception:
            pass
        if getattr(self, "supervisor", None) is None:
            self.supervisor = type("S", (), {"shutdown": lambda self: None})()
        for c in list(self.conns):
            self._close(c)
        if self.listener is not None:
            try:
                self.sel.unregister(self.listener)
            except (KeyError, ValueError):
                pass
            self.listener.close()
            self.listener = None
            for p in (self.socket_path,):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass
        self.supervisor.shutdown()
        if getattr(self, "tcc", None) is not None:
            self.tcc.shutdown()
        if getattr(self, "residents", None) is not None:
            self.residents.shutdown()
        if getattr(self, "yield_apps", None) is not None:
            self.yield_apps.shutdown()
        try:
            os.unlink(self.paths.pidfile)
        except FileNotFoundError:
            pass
        if self._wakeup_r is not None:
            signal.set_wakeup_fd(-1)
            for fd in (self._wakeup_r, self._wakeup_w):
                try:
                    self.sel.unregister(fd)
                except (KeyError, ValueError):
                    pass
                os.close(fd)
            self._wakeup_r = self._wakeup_w = None
        self.sel.close()
        if self.log is not None:
            self.log.close()
        self.store.close()
        self.store = None
        if self._lock_fh is not None:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
            finally:
                self._lock_fh.close()
            self._lock_fh = None
        os.umask(self._old_umask)

    def _write_pidfile(self) -> None:
        info = self.platform.proc(os.getpid())
        fd = os.open(self.paths.pidfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {info.start_sec if info else 0}\n")

    # ---- event sink / warnings ---------------------------------------------------------------------------------------
    def _event_sink(self, kind: str, job: str | None, body: dict) -> None:
        if self.store is not None:
            self.store.add_event(self.clock.now_ms(), kind, job, body)

    def _warn_cb(self, kind: str, body: dict) -> None:
        self.log.decision(kind, **body)

    def _on_unknown_key(self, key: str) -> None:
        if self.log is not None:
            self.log.decision("config_unknown_key", key=key)

    @staticmethod
    def _effective(cfg: Config) -> Config:
        """Resolve an unset timezone to the machine's civil zone once, so policy/ never reads the system."""
        if cfg.timezone is None:
            from gsched.config import system_timezone_name
            return cfg.replace(timezone=system_timezone_name())
        return cfg

    @staticmethod
    def _mtime(path: str):
        try:
            return os.stat(path).st_mtime_ns
        except FileNotFoundError:
            return None

    # ---- config / project reload --------------------------------------------------------------------------------------
    def reload_if_changed(self, now_ms: int | None = None) -> None:
        if not self._cfg_given and self._mtime(self.paths.config) != self._cfg_mtime:
            self.reload_config()
        if self.projects.reload_if_changed():
            self.sync_projects()
        self.residents.reload_if_changed()

    def reload_config(self) -> bool:
        self._cfg_mtime = self._mtime(self.paths.config)
        try:
            new = Config.load(self.paths.config, on_unknown=self._on_unknown_key, seen_unknown=self._cfg_unknown)
        except ConfigError as e:
            self.log.decision("config_reload_failed", key=e.key, error=str(e))
            return False
        self.cfg = self._effective(new)
        new = self.cfg
        self.budget_bytes = compute_budget(self.machine, new)
        self.cpu_budget = compute_cpu_budget(self.machine, new)
        self.log.set_max_bytes(new.log_max_bytes)
        self.log.decision("config_reload")
        return True

    def reload_all(self) -> None:
        if not self._cfg_given:
            self.reload_config()
        self.projects.reload_if_changed(force=True)
        self.sync_projects()
        self.residents.reload_if_changed(force=True)

    def sync_projects(self) -> None:
        names = set()
        for p in self.projects.all():
            self.store.upsert_project(p.name, p.weight, True)
            names.add(p.name)
        for r in self.store.projects():
            if r["id"] not in names and r["present"]:
                self.store.mark_project_absent(r["id"])

    # ---- loop ----------------------------------------------------------------------------------------------------------------
    def serve_forever(self) -> None:
        while not self.stopping:
            try:
                self.step(timeout=self._select_timeout())
            except Exception as e:
                # Last resort (16 section 3.1): a bug outside the guarded tick phases (a selector callback) must not
                # take the daemon down. Log it, back off briefly, carry on.
                import traceback
                self.log.decision("loop_error", error=repr(e), tb=traceback.format_exc().replace("\n", " | ")[-1500:])
                self.sel.select(0.2)

    def _select_timeout(self) -> float:
        return max(0.0, min(1.0, self.next_tick_mono - self.clock.mono()))

    def step(self, timeout: float = 0.0) -> None:
        if self.admit_hint:
            timeout = 0.0
        for key, _ev in self.sel.select(timeout):
            data: Key = key.data
            if data.kind == "listen":
                self._accept()
            elif data.kind == "conn":
                self._on_conn_readable(data.obj)
            elif data.kind == "report":
                self.supervisor.on_report_readable(data.obj)
            elif data.kind == "wakeup":
                self._drain_wakeup()
            elif data.kind == "probe":
                self.residents.on_probe_event(data.obj, _ev)
            elif data.kind == "tcc":
                self.tcc.on_event(data.obj)
            elif data.kind == "lookup":
                self.scanner.on_lookup_event(data.obj)
        self._expire_idle_connections()
        if self.clock.mono() >= self.next_tick_mono:
            self.admit_hint = False
            self.tick()
            self.next_tick_mono = self.clock.mono() + 1.0
        elif self.admit_hint:
            self.admit_hint = False
            self.tick(admission_only=True)

    def tick(self, admission_only: bool = False) -> None:
        self.ticker.run(admission_only)
        if not admission_only:
            self.ticker.phase("parked_liveness", self._check_parked_liveness)

    # ---- report pipes ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_key(kind: str, obj=None) -> Key:
        return Key(kind, obj)

    def register_report(self, fd: int, job_id: str) -> None:
        self.sel.register(fd, selectors.EVENT_READ, Key("report", job_id))

    def unregister_report(self, fd: int) -> None:
        try:
            self.sel.unregister(fd)
        except (KeyError, ValueError):
            pass

    # ---- connections --------------------------------------------------------------------------------------------------------------
    def _accept(self) -> None:
        while True:
            try:
                sock, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            sock.setblocking(False)
            if len(self.conns) >= MAX_CONNS:
                self._raw_send(sock, {"rpc": RPC, "ok": False, "error": {"code": "busy", "message": "too many connections"}})
                sock.close()
                continue
            try:
                uid, pid = self.platform.peer_credentials(sock)
            except OSError:
                sock.close()
                continue
            if uid != os.getuid():
                self._raw_send(sock, {"rpc": RPC, "ok": False, "error": {"code": "forbidden", "message": "peer uid mismatch"}})
                sock.close()
                continue
            c = Conn(sock, uid, pid, opened_mono=self.clock.mono())
            self.conns.append(c)
            self.sel.register(sock, selectors.EVENT_READ, Key("conn", c))

    @staticmethod
    def _raw_send(sock: socket.socket, env: dict) -> bool:
        try:
            sock.setblocking(True)
            sock.settimeout(1.0)
            sock.sendall(json.dumps(env, separators=(",", ":")).encode() + b"\n")
            return True
        except OSError:
            return False

    def _close(self, c: Conn) -> None:
        if c.closed:
            return
        c.closed = True
        try:
            self.sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass
        try:
            c.sock.close()
        except OSError:
            pass
        if c in self.conns:
            self.conns.remove(c)

    def send_and_close(self, c: Conn, env: dict) -> bool:
        ok = self._raw_send(c.sock, env)
        if not ok:
            self.log.decision("client_write_timeout", peer_pid=c.peer_pid)
        self._close(c)
        return ok

    def _on_conn_readable(self, c: Conn) -> None:
        if c.closed:
            return
        try:
            data = c.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:                       # EOF
            self._peer_gone(c)
            return
        if c.parked is not None:
            return                          # ignore extra bytes while parked
        c.buf += data
        if len(c.buf) > MAX_REQUEST:
            self.send_and_close(c, self._err("invalid", "request too large"))
            return
        nl = c.buf.find(b"\n")
        if nl < 0:
            return
        line = bytes(c.buf[:nl])
        c.buf.clear()
        self._dispatch(c, line)

    @staticmethod
    def _err(code: str, message: str, **detail) -> dict:
        e = {"code": code, "message": message}
        if detail:
            e["detail"] = detail
        return {"rpc": RPC, "ok": False, "error": e}

    def _dispatch(self, c: Conn, line: bytes) -> None:
        try:
            req = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError):
            self.send_and_close(c, self._err("schema", "request is not valid JSON"))
            return
        if not isinstance(req, dict) or req.get("rpc") != RPC or not isinstance(req.get("method"), str):
            self.send_and_close(c, self._err("schema", f"request must be an object with rpc={RPC!r} and a method"))
            return
        door = req.get("door", "cli")
        if door not in DOORS:
            self.send_and_close(c, self._err("invalid", f"unknown door {door!r}"))
            return
        c.door = door
        params = req.get("params") or {}
        if not isinstance(params, dict):
            self.send_and_close(c, self._err("invalid", "params must be an object"))
            return
        try:
            result = self.handlers.dispatch(c, req["method"], params, door)
        except GschedError as e:
            self.send_and_close(c, {"rpc": RPC, "ok": False, "error": e.to_dict()})
            return
        except Exception as e:  # a bug: log, keep running
            import traceback
            self.log.decision("internal_error", method=req["method"], error=repr(e),
                              tb=traceback.format_exc().replace("\n", " | ")[-1500:])
            self.send_and_close(c, self._err("internal", "internal error; see the daemon log"))
            return
        if c.parked is not None:
            return
        self.send_and_close(c, {"rpc": RPC, "ok": True, "result": result})

    def _expire_idle_connections(self) -> None:
        now = self.clock.mono()
        for c in list(self.conns):
            if c.parked is None and not c.closed and now - c.opened_mono > IDLE_TIMEOUT_S:
                self._close(c)

    # ---- parking (05 section 6) ---------------------------------------------------------------------------------------------------
    def park(self, c: Conn, kind: str, job: str, *, deadline_mono: float | None = None) -> None:
        row = self.store.get(job)
        c.parked = Parked(kind, job, deadline_mono, (row["state"], row["held_reason"]) if row else ("?", None), c.door)
        c.opened_mono = self.clock.mono()
        if kind == "wait_start":
            self.waiter_lost.pop(job, None)

    def start_waiters(self) -> set:
        return {c.parked.job for c in self.conns if c.parked is not None and c.parked.kind == "wait_start" and not c.closed}

    def _parked_for(self, job_id: str | None):
        return [c for c in list(self.conns) if c.parked is not None and not c.closed
                and (job_id is None or c.parked.job == job_id)]

    def notify_parked(self, job_id: str | None = None) -> None:
        for c in self._parked_for(job_id):
            p = c.parked
            row = self.store.get(p.job)
            if row is None:
                self.send_and_close(c, self._err("unknown_job", f"no such job {p.job}"))
                continue
            terminal = row["state"] in ("done", "failed", "cancelled", "lost")
            if p.kind == "wait_exit" and terminal:
                self.send_and_close(c, {"rpc": RPC, "ok": True, "result": self.handlers.view(row, door=p.door)})
            elif p.kind == "wait_start" and (terminal or (row["state"] == "held")):
                # a lease is delivered only by deliver_lease(); here the lease (if any) was already sent
                if terminal or row["held_reason"] != "await_attach":
                    self.send_and_close(c, {"rpc": RPC, "ok": True, "result": self.handlers.view(row, door=p.door)})
            elif p.kind == "get_wait":
                changed = (row["state"], row["held_reason"]) != p.last_seen
                expired = p.deadline_mono is not None and self.clock.mono() >= p.deadline_mono
                if changed or expired:
                    self.send_and_close(c, {"rpc": RPC, "ok": True, "result": self.handlers.view(row, door=p.door)})

    def deliver_lease(self, job_id: str, token: str) -> bool:
        """Called after commit_admit for an external job: the lease goes only to the parked start waiter."""
        for c in self._parked_for(job_id):
            if c.parked.kind == "wait_start":
                row = self.store.get(job_id)
                res = self.handlers.view(row, door=c.parked.door)
                res["lease"] = token
                ok = self.send_and_close(c, {"rpc": RPC, "ok": True, "result": res})
                if not ok:
                    self.log.decision("lease_undelivered", job=job_id)
                return ok
        self.log.decision("lease_undelivered", job=job_id)
        return False

    def _peer_gone(self, c: Conn) -> None:
        p = c.parked
        self._close(c)
        if p is None:
            return
        if p.kind == "wait_start":
            row = self.store.get(p.job)
            if row is not None and row["state"] == "queued" and p.job not in self.start_waiters():
                self.waiter_lost[p.job] = self.clock.mono()

    def _check_parked_liveness(self) -> None:
        for c in self._parked_for(None):
            gone = not self.platform.pid_alive(c.peer_pid)
            if not gone:
                try:
                    gone = c.sock.recv(1, socket.MSG_PEEK) == b""
                except (BlockingIOError, InterruptedError):
                    gone = False
                except OSError:
                    gone = True
            if gone:
                self._peer_gone(c)

    # ---- helpers used by the supervisor / handlers -----------------------------------------------------------------------------------
    def assign_queue_for_row(self, row):
        ns = types.SimpleNamespace(checkpoint=row["checkpoint"], estimate_s=row["estimate_s"],
                                   queue_requested=row["queue_requested"])
        q, st = assign_queue(ns, self.cfg)
        if q is None:
            q = row["queue"]
        return q, st

    def snapshot(self):
        if self.last_sample is None:
            self.last_sample = self.platform.mem_sample(self.clock.now_ms())
        return self.ticker.build_snapshot()


def run_daemon(home=None, *, foreground: bool = True) -> int:
    """Entry point for `gsched daemon`. Returns the process exit code."""
    d = Daemon(home)
    try:
        d.start()
    except (ConfigError, PlatformError) as e:
        print(f"gsched daemon: {e}", file=sys.stderr)
        return 78
    except DaemonBusy as e:
        print(f"gsched daemon: {e}", file=sys.stderr)
        return EXIT_BUSY
    except GschedError as e:
        print(f"gsched daemon: {e.message}", file=sys.stderr)
        return 78
    except Exception as e:
        from gsched.errors import StoreError
        if isinstance(e, StoreError):
            print(f"gsched daemon: {e}", file=sys.stderr)
            return 78
        raise
    d.install_signal_handlers()
    try:
        d.serve_forever()
    finally:
        d.stop()
    return 0
