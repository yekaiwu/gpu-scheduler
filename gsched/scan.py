"""Process classification (07 section 11). The scan NEVER signals anything."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from gsched.config import Config
from gsched.platform import Platform, ProcInfo
from gsched.units import GiB

OURS, FOREIGN_HEAVY, ORPHAN_SHIM, NOTED = "ours", "foreign_heavy", "orphan_shim", "noted"



def job_mem_bytes(p) -> int:
    """Memory a job-tree process holds: max(phys_footprint, resident). phys_footprint carries Metal/anon
    allocations that RSS misses; resident carries mmap'd file pages (llama.cpp maps GGUF weights) that
    phys_footprint misses. Measured live 2026-11-08: llama-server footprint 8.4 GiB, resident 26.1 GiB,
    GPU in-use 27.2 GiB. Used only for our own jobs, never for classifying foreign processes."""
    return max(p.footprint_bytes or 0, getattr(p, "resident_bytes", None) or 0)

@dataclass(frozen=True)
class Offender:
    cls: str                      # foreign_heavy | orphan_shim | noted
    key: tuple                    # ("proc", pid, start_sec) or ("port", port)
    pid: int | None
    path: str | None
    port: int | None
    footprint_bytes: int | None


@dataclass(frozen=True)
class ScanResult:
    trees: dict[str, frozenset[int]] = field(default_factory=dict)
    job_footprint: dict[str, int | None] = field(default_factory=dict)
    foreign_heavy: tuple = ()
    orphan_shims: tuple = ()
    noted: tuple = ()
    ports_listening: dict[int, bool] = field(default_factory=dict)
    by_pid: dict[int, ProcInfo] = field(default_factory=dict)
    ours: frozenset[int] = frozenset()
    gpu_busy_pct: int | None = None


def build_children(procs) -> dict[int, list[int]]:
    ch: dict[int, list[int]] = {}
    for p in procs:
        ch.setdefault(p.ppid, []).append(p.pid)
    return ch


def descendants(root: int, children: dict[int, list[int]]) -> set[int]:
    out, stack = set(), [root]
    while stack:
        for c in children.get(stack.pop(), ()):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


class Scanner:
    def __init__(self, platform: Platform, *, uid: int | None = None, daemon_pid: int | None = None,
                 jobs_dir: str | None = None):
        self.platform = platform
        # Only a shim whose --exit-file lies in THIS daemon's jobs dir can be our orphan. Shims of another
        # GSCHED_HOME (a test suite, a second install) are someone else's (live 2026-11-08: a pytest run put the
        # real daemon into protective mode for 30 s).
        self.jobs_dir = os.path.realpath(jobs_dir) if jobs_dir else None
        self.uid = os.getuid() if uid is None else uid
        self.daemon_pid = os.getpid() if daemon_pid is None else daemon_pid
        self._ports_last: dict[int, bool] = {}
        self._port_owner: dict[int, int | None] = {}
        # lsof lookups run as non-blocking helper processes (16 section 3.4): port -> (AsyncProc, started_mono)
        self._lookups: dict[int, tuple] = {}
        self.watch = None            # (register(fd, port), unregister(fd)) installed by the daemon's selector
        self._now_mono = 0.0
        self._port_probe_mono = -1e9
        self._port_rr = 0
        self._argv_cache: dict[tuple[int, int, int], bool] = {}

    # ---- ports -----------------------------------------------------------------------------------
    def _probe_ports(self, cfg: Config, now_mono: float) -> None:
        ports = list(cfg.heavy_listen_ports)
        for p in list(self._ports_last):
            if p not in ports:
                del self._ports_last[p]
        if not ports:
            return
        if now_mono - self._port_probe_mono < cfg.port_probe_s:
            return
        self._port_probe_mono = now_mono
        batch = ports
        if len(ports) > 4:  # round-robin to stay inside the tick budget (each probe may take up to 100 ms)
            n = 4
            batch = [ports[(self._port_rr + i) % len(ports)] for i in range(n)]
            self._port_rr = (self._port_rr + n) % len(ports)
            self._port_probe_mono = now_mono - cfg.port_probe_s + max(1.0, cfg.port_probe_s / 4)
        for port in batch:
            was = self._ports_last.get(port, False)
            now = self.platform.port_listening(port)
            self._ports_last[port] = now
            if now and not was:
                self._port_owner[port] = None          # filled in when the lookup answers
                self._start_lookup(port, now_mono)
            elif not now:
                self._port_owner.pop(port, None)
                self._end_lookup(port, kill=True)

    LOOKUP_TIMEOUT_S = 3.0

    def _start_lookup(self, port: int, now_mono: float) -> None:
        if port in self._lookups:
            return
        try:
            proc = self.platform.port_owner_start(port)
        except OSError:
            return
        self._lookups[port] = (proc, now_mono)
        if self.watch is not None:
            try:
                self.watch[0](proc.fileno(), port)
            except (ValueError, OSError, KeyError):
                pass
        self._poll_lookup(port)

    def _end_lookup(self, port: int, kill: bool = False) -> None:
        ent = self._lookups.pop(port, None)
        if ent is None:
            return
        proc = ent[0]
        if self.watch is not None:
            try:
                self.watch[1](proc.fileno())
            except (ValueError, OSError, KeyError, TypeError):
                pass
        if kill:
            self.platform.kill_async(proc)
        proc.close()

    def _poll_lookup(self, port: int) -> None:
        ent = self._lookups.get(port)
        if ent is None:
            return
        res = ent[0].poll()
        if res is None:
            return
        _rc, out = res
        pids = [int(x) for x in out.split() if x.isdigit()]
        self._end_lookup(port)
        if port in self._ports_last and self._ports_last[port]:
            self._port_owner[port] = pids[0] if pids else None

    def on_lookup_event(self, port: int) -> None:
        self._poll_lookup(port)

    def poll_lookups(self, now_mono: float) -> None:
        """Every classify: collect finished lookups, kill the ones that take longer than 3 s."""
        for port in list(self._lookups):
            self._poll_lookup(port)
            ent = self._lookups.get(port)
            if ent is not None and now_mono - ent[1] >= self.LOOKUP_TIMEOUT_S:
                self._end_lookup(port, kill=True)

    # ---- classification ----------------------------------------------------------------------------
    def classify(self, procs: list[ProcInfo], running_rows, cfg: Config, now_mono: float = 0.0,
                 gpu_util_pct: int | None = None) -> ScanResult:
        by_pid = {p.pid: p for p in procs}
        children = build_children(procs)
        trees: dict[str, frozenset[int]] = {}
        fp: dict[str, int | None] = {}
        ours: set[int] = set()
        known_shims: set[int] = set()
        our_listen_ports: set[int] = set()
        for r in running_rows:
            if r["pid"] is None:
                continue
            tree: set[int] = set()
            shim = r["shim_pid"]
            if r["spawn"] == "external":
                if r["pid"] in by_pid:
                    tree = {r["pid"]} | descendants(r["pid"], children)
            else:
                pg = r["pgid"]
                tree = {p.pid for p in procs if pg is not None and p.pgid == pg}
                if shim is not None and shim in by_pid:
                    tree |= {shim} | descendants(shim, children)
                    known_shims.add(shim)
                    ours.add(shim)
                if r["pid"] in by_pid:
                    tree |= {r["pid"]} | descendants(r["pid"], children)
            trees[r["id"]] = frozenset(tree)
            ours |= tree
            if r["listen_port"]:
                our_listen_ports.add(r["listen_port"])
            wl = by_pid.get(r["pid"])
            if wl is None or wl.footprint_bytes is None:
                fp[r["id"]] = None
            else:
                fp[r["id"]] = sum(job_mem_bytes(by_pid[x]) for x in tree
                                  if x in by_pid and x != shim)
        # the daemon and its non-job descendants (probe subprocesses)
        if self.daemon_pid in by_pid:
            ours |= {self.daemon_pid} | descendants(self.daemon_pid, children)
        heavy, orphans, noted = [], [], []
        note_bytes = int(cfg.footprint_note_gib * GiB)
        heavy_names = set(cfg.heavy_basenames)
        for p in procs:
            if p.pid in ours or p.pid <= 1:
                continue
            base = os.path.basename(p.path) if p.path else None
            if base and base in heavy_names:
                heavy.append(Offender(FOREIGN_HEAVY, ("proc", p.pid, p.start_sec), p.pid, p.path, None,
                                      p.footprint_bytes))
                continue
            if p.uid == self.uid and base and base.startswith("python") and p.pid not in known_shims:
                if self._is_shim(p):
                    orphans.append(Offender(ORPHAN_SHIM, ("proc", p.pid, p.start_sec), p.pid, p.path, None,
                                            p.footprint_bytes))
                    continue
            if p.footprint_bytes is not None and p.footprint_bytes > note_bytes:
                noted.append(Offender(NOTED, ("proc", p.pid, p.start_sec), p.pid, p.path, None, p.footprint_bytes))
        self.poll_lookups(now_mono)
        self._probe_ports(cfg, now_mono)
        for port, listening in self._ports_last.items():
            if listening and port not in our_listen_ports:
                heavy.append(Offender(FOREIGN_HEAVY, ("port", port), self._port_owner.get(port), None, port, None))
        return ScanResult(trees=trees, job_footprint=fp, foreign_heavy=tuple(heavy), orphan_shims=tuple(orphans),
                          noted=tuple(noted), ports_listening=dict(self._ports_last), by_pid=by_pid,
                          ours=frozenset(ours), gpu_busy_pct=gpu_util_pct)

    def _is_shim(self, p: ProcInfo) -> bool:
        key = (p.pid, p.start_sec, p.start_usec)
        v = self._argv_cache.get(key)
        if v is None:
            argv = self.platform.proc_argv(p.pid)
            v = bool(argv) and "gsched.shim" in argv
            if v and self.jobs_dir is not None:
                try:
                    ef = argv[argv.index("--exit-file") + 1]
                except (ValueError, IndexError):
                    ef = None
                v = ef is not None and os.path.realpath(ef).startswith(self.jobs_dir + os.sep)
            if len(self._argv_cache) > 5000:
                self._argv_cache.clear()
            self._argv_cache[key] = v
        return v


class ProtectiveState:
    """Protective mode (07 section 11): enter on any foreign-heavy/orphan-shim sighting; clear after
    `protective_clear_s` of continuous absence. A drop in footprint never clears it."""

    def __init__(self):
        self.active = False
        self.empty_since_mono: float | None = None
        self.busy_since_mono: float | None = None
        self.reasons: list = []

    def update(self, scan: ScanResult, cfg: Config, now_mono: float, gpu_job_running: bool) -> tuple[bool, bool]:
        """Returns (entered, exited) for this update."""
        present = list(scan.foreign_heavy) + list(scan.orphan_shims)
        gpu_busy = False
        if cfg.gpu_busy_foreign_pct is not None and scan.gpu_busy_pct is not None and not gpu_job_running \
                and scan.gpu_busy_pct > cfg.gpu_busy_foreign_pct:
            if self.busy_since_mono is None:
                self.busy_since_mono = now_mono
            gpu_busy = now_mono - self.busy_since_mono >= 30
        else:
            self.busy_since_mono = None
        self.reasons = present
        entered = exited = False
        if present or gpu_busy:
            self.empty_since_mono = None
            if not self.active:
                self.active = True
                entered = True
        elif self.active:
            if self.empty_since_mono is None:
                self.empty_since_mono = now_mono
            if now_mono - self.empty_since_mono >= cfg.protective_clear_s:
                self.active = False
                self.empty_since_mono = None
                exited = True
        return entered, exited
