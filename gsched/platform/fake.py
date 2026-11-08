"""In-memory deterministic Platform used by almost every test (02 section 5).

The fake does not start real processes. It simulates the shim protocol over REAL pipes:
the report pipe carries the workload pid, the gate pipe is polled for the release byte,
and `exit.json` is written into the real job dir when the workload ends. The daemon's
supervision code therefore runs unchanged, including the files on disk.

This module sends no real signals. Delivery is modelled in memory and recorded in
`FakePlatform.signals` as `(pid_or_-pgid, signo, job_id_or_"")`.
"""

from __future__ import annotations

import json
import os
import signal as _signal
import time
from dataclasses import dataclass, field

from gsched.platform import GpuStats, JobIdentity, MachineInfo, MemSample, ProcInfo
from gsched.units import GiB, gib_to_bytes

SIGKILL = int(_signal.SIGKILL)
SIGTERM = int(_signal.SIGTERM)
SIGUSR1 = int(_signal.SIGUSR1)
SIGUSR2 = int(_signal.SIGUSR2)
BASE_START_SEC = 1_790_000_000


@dataclass
class _Rec:
    pid: int
    ppid: int
    pgid: int
    comm: str
    path: str | None
    uid: int
    start_sec: int
    start_usec: int = 0
    footprint: int | None = 0
    resident: int | None = None
    unreadable: bool = False
    port: int | None = None
    kind: str = "foreign"          # foreign | shim | workload | child
    job_id: str = ""
    argv: list | None = None


class FakeWorkload:
    """Handle for a simulated workload; tests script its behavior."""

    def __init__(self, plat: "FakePlatform", shim: "_FakeShim", rec: _Rec):
        self._plat = plat
        self._shim = shim
        self.rec = rec
        self.job_id = shim.job_id
        self.behaviors: dict[int, tuple[str, int]] = {}
        self.listen_port: int | None = None
        self.children: list[int] = []
        self.exited = False
        self.released = False
        self.peak = 0

    @property
    def pid(self) -> int:
        return self.rec.pid

    @property
    def command(self):
        return self._shim.command

    @property
    def env(self):
        return self._shim.env

    def on_signal(self, sig, action: str, code: int = 0) -> None:
        """action: checkpoint_and_exit | ignore | exit | die"""
        self.behaviors[int(sig)] = (action, code)

    def exit(self, code: int = 0, *, write_record: bool = True) -> None:
        self._plat._finish(self, code=code, write_record=write_record)

    def die(self, sig: int = SIGKILL) -> None:
        self._plat._finish(self, sig=int(sig))

    def set_footprint(self, gib: float) -> None:
        self.rec.footprint = gib_to_bytes(gib)
        self.peak = max(self.peak, self.rec.footprint)

    def set_resident(self, gib: float) -> None:
        """Simulate mmap'd file pages (e.g. GGUF weights): counted in resident, not phys_footprint."""
        self.rec.resident = gib_to_bytes(gib)

    def spawn_child(self, name: str, gib: float, *, setsid: bool = False, path: str | None = None) -> int:
        pid = self._plat._new_pid()
        rec = _Rec(pid, self.rec.pid, pid if setsid else self.rec.pgid, name[:15],
                   path or f"/opt/fake/bin/{name}", os.getuid(), BASE_START_SEC + pid, footprint=gib_to_bytes(gib),
                   kind="child", job_id=self.job_id)
        self._plat._procs[pid] = rec
        self.children.append(pid)
        return pid

    def write_stdout(self, text: str) -> None:
        with open(self._shim.stdout_path, "a") as fh:
            fh.write(text)


@dataclass
class _FakeShim:
    job_id: str
    rec: _Rec
    gate_fd: int
    exit_file: str
    command: list
    env: dict
    cwd: str
    stdout_path: str
    stderr_path: str
    workload: FakeWorkload | None = None
    state: str = "waiting"        # waiting | running | done
    report_fd: int | None = None
    target_cwd: str | None = None   # the job's real cwd (--cwd); the shim process itself starts in the job dir


class _FakeAsync:
    """Fake of platform.AsyncProc over a real pipe so the daemon's selector registration is exercised. A delay of None
    means it never finishes on its own (a probe stuck behind a TCC consent prompt)."""

    def __init__(self, plat: "FakePlatform", argv: list, rc: int, out: str, delay_s: float | None):
        self._plat, self.argv, self._rc, self._out = plat, argv, rc, out
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self._ready_ms = None if delay_s is None else plat._now_ms() + int(delay_s * 1000)
        self.done = False
        self.killed = False
        self._closed = False
        if delay_s == 0:
            self._finish()

    def _finish(self) -> None:
        if self.done:
            return
        self.done = True
        if self._w is not None:
            try:
                os.write(self._w, self._out.encode())
            except OSError:
                pass
            os.close(self._w)
            self._w = None

    def fileno(self) -> int:
        return self._r

    def poll(self):
        if not self.done and self._ready_ms is not None and self._plat._now_ms() >= self._ready_ms:
            self._finish()
        if not self.done:
            return None
        return self._rc, self._out

    def kill(self) -> None:
        if self.done:
            return
        self.killed = True
        self._rc, self._out = -9, ""
        self._plat.async_killed.append(self.argv)
        self._finish()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._r, self._w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._r = self._w = None


class FakePlatform:
    def __init__(self, memsize_gib: float = 48, page_size: int = 16384, cpu: int = 18, clock=None):
        self._memsize = gib_to_bytes(memsize_gib)
        self._page = page_size
        self._cpu = cpu
        self.clock = clock
        self._machine = MachineInfo(self._memsize, page_size, cpu, None, None, "Fake Silicon", "27.0", 0)
        self._procs: dict[int, _Rec] = {}
        self._next_pid = 1000
        self._shims: dict[str, _FakeShim] = {}
        self._workloads: dict[str, FakeWorkload] = {}
        self._reaped: list[tuple[int, int]] = []
        self.signals: list[tuple[int, int, str]] = []
        self.caffeinated: list[int] = []
        self.auto_reclaim = True
        self._base_reclaim = gib_to_bytes(memsize_gib * 0.8)
        self._level: int | None = 80
        self._swap_used = 0
        self._swap_total = gib_to_bytes(4)
        self._swap_unreadable = False
        self.peer_uid: int | None = None
        self.peer_pid: int | None = None
        self.spawn_error: Exception | None = None
        self.suppress_report = False
        self.gate_write_blocked = False
        self.shim_dies_on_group_kill = True
        self.gpu = GpuStats(None, None, None)
        self.spawned: list[str] = []
        self.signal_hook = None
        self._caff: dict[int, int] = {}
        # TCC simulation (16 / urgent addendum): root -> "ok" | "denied" | "absent" | "blocked" (never answers,
        # like an open() stuck behind a consent prompt) | "slow:<seconds>" (answers ok after that long).
        self.tcc: dict[str, str] = {}
        self.blocked_cwds: set = set()          # chdir() into these hangs (the workload never reports cwd_ok)
        self.async_spawned: list[list[str]] = []
        self.async_killed: list[list[str]] = []
        self.async_spawn_error: Exception | None = None
        self.port_lookup_delay_s = 0.0
        self.async_hook = None        # callable(argv) -> (rc, output, delay_s | None) or None for the default answer

    # ---- time / pids -------------------------------------------------------------
    def _now_ms(self) -> int:
        return self.clock.now_ms() if self.clock else int(time.time() * 1000)

    def _new_pid(self) -> int:
        self._next_pid += 1
        while self._next_pid in self._procs or self._next_pid == os.getpid():
            self._next_pid += 1
        return self._next_pid

    # ---- knobs -----------------------------------------------------------------------
    def set_mem(self, *, reclaimable_gib=None, level=..., swap_used_gib=None, swap_unreadable=None,
                swap_total_gib=None) -> None:
        if reclaimable_gib is not None:
            self._base_reclaim = gib_to_bytes(reclaimable_gib)
        if level is not ...:
            self._level = level
        if swap_used_gib is not None:
            self._swap_used = gib_to_bytes(swap_used_gib)
        if swap_total_gib is not None:
            self._swap_total = gib_to_bytes(swap_total_gib)
        if swap_unreadable is not None:
            self._swap_unreadable = swap_unreadable

    def add_foreign(self, name: str, footprint_gib: float, *, path: str | None = None, port: int | None = None,
                    uid: int | None = None, ppid: int = 1, argv: list | None = None) -> int:
        pid = self._new_pid()
        self._procs[pid] = _Rec(pid, ppid, pid, name[:15], path if path is not None else f"/opt/fake/bin/{name}",
                                os.getuid() if uid is None else uid, BASE_START_SEC + pid,
                                footprint=gib_to_bytes(footprint_gib), port=port, argv=argv)
        return pid

    def remove(self, pid: int) -> None:
        self._procs.pop(pid, None)

    def set_footprint(self, pid: int, gib: float) -> None:
        self._procs[pid].footprint = gib_to_bytes(gib)

    def set_footprint_unreadable(self, pid: int, flag: bool = True) -> None:
        self._procs[pid].unreadable = flag

    def recycle_pid(self, pid: int) -> None:
        """Same pid, new start time: every identity check against the old one now fails."""
        r = self._procs[pid]
        r.start_usec += 1
        r.start_sec += 1

    def workload(self, job_id: str) -> FakeWorkload:
        return self._workloads[job_id]

    def kill_shim(self, job_id: str) -> None:
        """The shim dies without writing exit.json; its workload keeps running (reparented to pid 1)."""
        sh = self._shims[job_id]
        self._procs.pop(sh.rec.pid, None)
        cp = self._caff.pop(sh.rec.pid, None)
        if cp is not None:
            self._procs.pop(cp, None)
        if sh.workload and not sh.workload.exited:
            sh.workload.rec.ppid = 1
        sh.state = "done"
        self._reaped.append((sh.rec.pid, 9))

    # ---- Platform protocol: machine / memory --------------------------------------------
    def machine(self) -> MachineInfo:
        return self._machine

    def layout_report(self) -> dict:
        return {"ok": True, "fake": True}

    def _tracked_footprint(self) -> int:
        return sum(r.footprint or 0 for r in self._procs.values())

    def mem_sample(self, now_ms: int) -> MemSample:
        rec = self._base_reclaim
        if self.auto_reclaim:
            rec = max(0, rec - self._tracked_footprint())
        pages = rec // self._page
        return MemSample(
            t_ms=now_ms, free_pages=pages, purgeable_pages=0, file_backed_pages=0, anonymous_pages=0,
            wired_pages=0, compressor_pages=0, page_size=self._page, level=self._level,
            swap_used_bytes=None if self._swap_unreadable else self._swap_used,
            swap_total_bytes=None if self._swap_unreadable else self._swap_total)

    # ---- processes ----------------------------------------------------------------------------
    def _info(self, r: _Rec) -> ProcInfo:
        fp = None if r.unreadable else r.footprint
        res = None if r.unreadable else r.resident
        return ProcInfo(r.pid, r.ppid, r.pgid, r.uid, r.start_sec, r.start_usec, r.comm, r.path, fp, fp, res)

    def processes(self) -> list[ProcInfo]:
        self._poll()
        return [self._info(r) for r in self._procs.values()]

    def proc(self, pid: int) -> ProcInfo | None:
        self._poll()
        r = self._procs.get(pid)
        return self._info(r) if r else None

    def proc_argv(self, pid: int) -> list[str] | None:
        r = self._procs.get(pid)
        if r is None:
            return None
        return r.argv

    def identity_matches(self, ident: JobIdentity) -> bool:
        r = self._procs.get(ident.pid)
        if r is None:
            return False
        if r.start_sec != ident.start_sec or r.start_usec != ident.start_usec:
            return False
        if ident.path is not None and r.path is not None and r.path != ident.path:
            return False
        return True

    def pid_alive(self, pid: int) -> bool:
        return pid in self._procs or pid == os.getpid()

    # ---- signals (recorded, never real) -------------------------------------------------------------
    def signal_pid(self, ident: JobIdentity, sig: int) -> bool:
        self._poll()
        if not self.identity_matches(ident):
            return False
        r = self._procs[ident.pid]
        self.signals.append((ident.pid, int(sig), r.job_id))
        self._deliver(r, int(sig), group=False)
        return True

    def signal_group(self, ident: JobIdentity, sig: int) -> bool:
        self._poll()
        if not self.identity_matches(ident):
            return False
        r = self._procs[ident.pid]
        if r.pgid != ident.pgid or ident.pgid <= 1:
            return False
        self.signals.append((-ident.pgid, int(sig), r.job_id))
        members = [m for m in list(self._procs.values()) if m.pgid == ident.pgid]
        for m in members:
            if m.pid in self._procs:
                self._deliver(m, int(sig), group=True)
        return True

    def _deliver(self, r: _Rec, sig: int, *, group: bool) -> None:
        if self.signal_hook:
            self.signal_hook(r, sig)
        if r.kind == "foreign":
            if sig in (SIGTERM, SIGKILL):
                self._procs.pop(r.pid, None)
            return
        if r.kind == "child":
            if sig in (SIGTERM, SIGKILL):
                self._procs.pop(r.pid, None)
            return
        if r.kind == "shim":
            if sig == SIGKILL and group and self.shim_dies_on_group_kill:
                self.kill_shim(r.job_id)
            return  # shim ignores everything else (no-op handlers)
        w = self._workloads.get(r.job_id)
        if r.kind != "workload" or w is None or w.exited:
            return
        beh = w.behaviors.get(sig)
        if sig == SIGKILL:
            beh = ("die", SIGKILL)
        if beh is None:
            beh = ("die", sig) if sig == SIGTERM else ("ignore", 0)
        action, code = beh
        if action == "ignore":
            return
        if action == "exit":
            self._finish(w, code=code)
        elif action == "die":
            self._finish(w, sig=sig)
        elif action == "checkpoint_and_exit":
            ck = w.env.get("GSCHED_CHECKPOINT_DIR")
            if ck:
                os.makedirs(ck, exist_ok=True)
                p = os.path.join(ck, "READY")
                with open(p, "w") as fh:
                    fh.write("ready\n")
                ts = self._now_ms() / 1000
                os.utime(p, (ts, ts))
            self._finish(w, code=0)

    # ---- sockets / ports ------------------------------------------------------------------------------
    def peer_credentials(self, sock) -> tuple[int, int]:
        return (os.getuid() if self.peer_uid is None else self.peer_uid,
                os.getpid() if self.peer_pid is None else self.peer_pid)

    def port_listening(self, port: int) -> bool:
        if any(r.port == port for r in self._procs.values()):
            return True
        return any(w.listen_port == port and not w.exited for w in self._workloads.values())

    def port_owner_pids(self, port: int) -> list[int]:
        return [r.pid for r in self._procs.values() if r.port == port]

    def port_owner_start(self, port: int) -> "_FakeAsync":
        argv = ["lsof", f"-iTCP:{port}"]
        self.async_spawned.append(argv)
        out = "".join(f"{r.pid}\n" for r in self._procs.values() if r.port == port)
        return _FakeAsync(self, argv, 0, out, self.port_lookup_delay_s)

    def kill_async(self, proc) -> None:
        proc.kill()

    def spawn_async(self, argv: list[str]) -> "_FakeAsync":
        if self.async_spawn_error is not None:
            raise self.async_spawn_error
        argv = list(argv)
        self.async_spawned.append(argv)
        if self.async_hook is not None:
            ans = self.async_hook(argv)
            if ans is not None:
                return _FakeAsync(self, argv, *ans)
        if "os.listdir" in " ".join(argv):           # the TCC probe (gsched.tcc)
            root = argv[-1]
            mode = self.tcc.get(root, "ok")
            if mode == "ok":
                return _FakeAsync(self, argv, 0, "ok\n", 0.0)
            if mode == "denied":
                return _FakeAsync(self, argv, 1, "PermissionError: [Errno 1] Operation not permitted\n", 0.0)
            if mode == "absent":
                return _FakeAsync(self, argv, 1, "FileNotFoundError: [Errno 2] No such file or directory\n", 0.0)
            if mode.startswith("slow:"):
                return _FakeAsync(self, argv, 0, "ok\n", float(mode[5:]))
            return _FakeAsync(self, argv, 0, "ok\n", None)        # blocked: never answers until killed
        return _FakeAsync(self, argv, 0, "", 0.0)

    def gpu_stats(self) -> GpuStats:
        return self.gpu

    def caffeinate(self, pid: int) -> int | None:
        cp = self._new_pid()
        self._procs[cp] = _Rec(cp, os.getpid(), cp, "caffeinate", "/usr/bin/caffeinate", os.getuid(),
                               BASE_START_SEC + cp, footprint=1 << 20, kind="caff")
        self._caff[pid] = cp
        self.caffeinated.append(pid)
        return cp

    # ---- shim simulation ------------------------------------------------------------------------------------
    def spawn_shim(self, argv, *, env, cwd, stdout_path, stderr_path, pass_fds) -> int:
        if self.spawn_error is not None:
            raise self.spawn_error
        a = list(argv)
        def opt(name):
            return a[a.index(name) + 1]
        job_id, gate_fd, report_fd, exit_file = opt("--job"), int(opt("--gate-fd")), int(opt("--report-fd")), opt("--exit-file")
        command = a[a.index("--") + 1:]
        for p in (stdout_path, stderr_path):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        shim_pid = self._new_pid()
        shim_rec = _Rec(shim_pid, os.getpid(), shim_pid, "python3", a[0], os.getuid(), BASE_START_SEC + shim_pid,
                        footprint=20 * 2**20, kind="shim", job_id=job_id, argv=a)
        self._procs[shim_pid] = shim_rec
        wl_pid = self._new_pid()
        wl_rec = _Rec(wl_pid, shim_pid, shim_pid, "python3", a[0], os.getuid(), BASE_START_SEC + wl_pid,
                      footprint=0, kind="workload", job_id=job_id)
        self._procs[wl_pid] = wl_rec
        target = a[a.index("--cwd") + 1] if "--cwd" in a else None
        shim = _FakeShim(job_id, shim_rec, os.dup(gate_fd), exit_file, command, dict(env), cwd, stdout_path, stderr_path,
                         target_cwd=target)
        os.set_blocking(shim.gate_fd, False)
        w = FakeWorkload(self, shim, wl_rec)
        shim.workload = w
        self._shims[job_id] = shim
        self._workloads[job_id] = w
        self.spawned.append(job_id)
        rep = os.dup(report_fd)
        shim.report_fd = rep          # the workload keeps it until chdir succeeded (then it says cwd_ok)
        if not self.suppress_report:  # suppress: no data, no EOF
            os.write(rep, f"{wl_pid}\n".encode())
        return shim_pid

    def _poll(self) -> None:
        """Advance shims: read the gate pipe without blocking."""
        for sh in list(self._shims.values()):
            if sh.state != "waiting":
                continue
            try:
                b = os.read(sh.gate_fd, 1)
            except BlockingIOError:
                continue
            except OSError:
                b = b""
            w = sh.workload
            if b == b"1":
                sh.state = "running"
                w.released = True
                if sh.target_cwd in self.blocked_cwds:
                    pass          # chdir() hangs (TCC consent): never says cwd_ok, the pipe stays open
                elif sh.report_fd is not None:
                    try:
                        os.write(sh.report_fd, b"cwd_ok\n")
                        os.close(sh.report_fd)
                    except OSError:
                        pass
                    sh.report_fd = None
                cmd0 = sh.command[0] if sh.command else "true"
                w.rec.path = cmd0 if "/" in cmd0 else f"/opt/fake/bin/{cmd0}"
                w.rec.comm = os.path.basename(w.rec.path)[:15]
                w.rec.start_usec = w.rec.start_usec  # exec keeps the start time
                w.rec.argv = list(sh.command)
            elif b == b"":
                self._finish(w, code=111)

    def _finish(self, w: FakeWorkload, *, code: int | None = None, sig: int | None = None,
                write_record: bool = True) -> None:
        if w.exited:
            return
        w.exited = True
        sh = w._shim
        sh.state = "done"
        for pid in [w.rec.pid] + w.children:
            self._procs.pop(pid, None)
        shim_alive = sh.rec.pid in self._procs
        if write_record and shim_alive:
            rec = {"v": 1, "job": sh.job_id, "pid": w.rec.pid, "t_ms": self._now_ms(),
                   "peak_footprint_bytes": w.peak or None}
            if sig is not None:
                rec["signal"] = int(sig)
            else:
                rec["exit_code"] = int(code or 0)
            tmp = sh.exit_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(rec, fh)
            os.replace(tmp, sh.exit_file)
        cp = self._caff.pop(sh.rec.pid, None)
        if cp is not None:
            self._procs.pop(cp, None)
        if shim_alive:
            self._procs.pop(sh.rec.pid, None)
            status = ((code or 0) & 0xFF) << 8 if sig is None else int(sig)
            self._reaped.append((sh.rec.pid, status))
        try:
            os.close(sh.gate_fd)
        except OSError:
            pass
        if sh.report_fd is not None:
            try:
                os.close(sh.report_fd)
            except OSError:
                pass
            sh.report_fd = None

    def reap(self) -> list[tuple[int, int]]:
        self._poll()
        out, self._reaped = self._reaped, []
        return out
