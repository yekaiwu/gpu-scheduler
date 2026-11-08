"""Resource fit and the memory ledger (06 section 5)."""

from __future__ import annotations

from dataclasses import dataclass

from gsched.config import Config
from gsched.platform import MachineInfo
from gsched.policy import reservations
from gsched.policy.model import Snapshot, running
from gsched.units import GiB, gib_to_bytes


def compute_budget(machine: MachineInfo, cfg: Config) -> int:
    """budget = memsize - os_reserve (default max(8, 12.5 % of memsize))."""
    if cfg.os_reserve_gib is None:
        reserve = max(gib_to_bytes(8), machine.memsize_bytes // 8)
    else:
        reserve = gib_to_bytes(cfg.os_reserve_gib)
    return max(0, machine.memsize_bytes - reserve)


def compute_cpu_budget(machine: MachineInfo, cfg: Config) -> int:
    return cfg.cpu_budget if cfg.cpu_budget is not None else machine.physical_cpu


@dataclass(frozen=True)
class Ledger:
    reserved_bytes: int
    pending_bytes: int
    gpu_exclusive: int
    gpu_shared: int
    cpu_used: int
    locks: frozenset
    holders: tuple = ()       # (job_id, ...) for explanations


def ledger(snap: Snapshot, ignoring: frozenset = frozenset()) -> Ledger:
    reserved = pending = ex = sh = cpu = 0
    locks: set = set()
    holders = []
    for j in running(snap):
        if j.id in ignoring:
            continue
        fp = j.footprint_bytes
        reserved += max(j.memory_bytes, fp or 0)
        pending += 0 if fp is None else max(0, j.memory_bytes - fp)
        if j.gpu == "exclusive":
            ex += 1
        elif j.gpu == "shared":
            sh += 1
        cpu += j.cpu_cores
        locks |= j.locks
        holders.append(j.id)
    return Ledger(reserved, pending, ex, sh, cpu, frozenset(locks), tuple(holders))


def fits(j, snap: Snapshot, cfg: Config, led: Ledger | None = None, *,
         ignoring: frozenset = frozenset()) -> tuple[bool, list[str]]:
    """Returns (ok, blocked_by list in the documented order).

    `ignoring`: job ids whose resources are treated as already released (preemption planning). Their current
    footprint is added back to live memory at 0.9x, an intentional under-estimate."""
    if ignoring:
        led = ledger(snap, ignoring)
        addback = int(0.9 * sum((x.footprint_bytes or 0) for x in running(snap) if x.id in ignoring))
    else:
        led = led or ledger(snap)
        addback = 0
    reasons: list[str] = []
    if snap.mem.swap_used_bytes is None:
        reasons.append("swap_unknown")
    if snap.pressure != "normal":
        reasons.append("pressure")
    if snap.protective and j.gpu != "none":
        reasons.append("protective")
    if j.level_min is not None and (snap.mem.level is None or snap.mem.level < j.level_min):
        reasons.append("level")
    if led.reserved_bytes + j.memory_bytes > snap.budget_bytes:
        reasons.append("memory_ledger")
    live = snap.mem.reclaimable_bytes + addback - led.pending_bytes - gib_to_bytes(cfg.headroom_gib)
    if live < j.memory_bytes:
        reasons.append("memory_live")
    if j.gpu == "exclusive" and (led.gpu_exclusive or led.gpu_shared):
        reasons.append("gpu")
    if j.gpu == "shared" and (led.gpu_exclusive or led.gpu_shared >= cfg.gpu_shared_max):
        reasons.append("gpu")
    if led.cpu_used + j.cpu_cores > snap.cpu_budget:
        reasons.append("cpu")
    for l in sorted(j.locks & led.locks):
        reasons.append(f"lock:{l}")
    if cfg.reservations:
        reasons += reservations.blocking(j, snap, cfg, led, live, reasons, ignoring)
    return (not reasons), reasons


def live_free_bytes(snap: Snapshot, cfg: Config, led: Ledger | None = None) -> int:
    led = led or ledger(snap)
    return snap.mem.reclaimable_bytes - led.pending_bytes - gib_to_bytes(cfg.headroom_gib)
