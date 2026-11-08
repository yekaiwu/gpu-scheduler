"""Time reservations (16 section 2). Pure: windows are computed from `now_ms` and the config only.

A reservation holds memory (and optionally the GPU) for one project during a weekly window. Jobs of other projects
are affected while the window is active, and also before it opens when they would still be running when it does.
"""

from __future__ import annotations

from dataclasses import dataclass

from gsched.config import Config
from gsched.policy import windows
from gsched.policy.model import JobView, Plan, Snapshot, running
from gsched.units import GiB, gib_to_bytes

LATCH_PREFIX = "reservation_latch:"


@dataclass(frozen=True, slots=True)
class Reservation:
    name: str
    days: tuple
    start: str
    end: str
    project: str
    memory_bytes: int
    gpu: str = "none"
    unload_restartable: bool = False


@dataclass(frozen=True, slots=True)
class Occurrence:
    start_ms: int
    end_ms: int
    date: str            # civil date the window starts on (the key of the per-day latch)


def parse(cfg: Config) -> tuple:
    return tuple(Reservation(name=r["name"], days=tuple(r["days"]), start=r["start"], end=r["end"],
                             project=r["project"], memory_bytes=gib_to_bytes(r["memory_gib"]),
                             gpu=r.get("gpu", "none"), unload_restartable=bool(r.get("unload_restartable", False)))
                 for r in cfg.reservations)


def occurrences(r: Reservation, now_ms: int, cfg: Config, **kw) -> list:
    return [Occurrence(*o) for o in windows.window_occurrences(r.days, r.start, r.end, now_ms, cfg, **kw)]


def current(r: Reservation, now_ms: int, cfg: Config) -> Occurrence | None:
    for o in occurrences(r, now_ms, cfg):
        if o.start_ms <= now_ms < o.end_ms:
            return o
    return None


def next_start(r: Reservation, now_ms: int, cfg: Config) -> Occurrence | None:
    """The first window that starts strictly after now."""
    for o in occurrences(r, now_ms, cfg):
        if o.start_ms > now_ms:
            return o
    return None


def active(now_ms: int, cfg: Config) -> list:
    """The reservations whose window contains `now`."""
    return [r for r in parse(cfg) if current(r, now_ms, cfg) is not None]


def upcoming(now_ms: int, cfg: Config, horizon_ms: int) -> list:
    """(reservation, window) for windows starting within the horizon."""
    out = []
    for r in parse(cfg):
        o = next_start(r, now_ms, cfg)
        if o is not None and o.start_ms - now_ms <= horizon_ms:
            out.append((r, o))
    return out


def held_owner_bytes(r: Reservation, snap: Snapshot, ignoring: frozenset = frozenset()) -> int:
    return sum(j.memory_bytes for j in running(snap) if j.project == r.project and j.id not in ignoring)


def reserve_remaining(r: Reservation, snap: Snapshot, ignoring: frozenset = frozenset()) -> int:
    """Memory the reservation still holds back: what it reserves minus what the owner's held jobs already took."""
    return max(0, r.memory_bytes - held_owner_bytes(r, snap, ignoring))


def applies(j: JobView, r: Reservation, now_ms: int, cfg: Config) -> bool:
    """Does reservation `r` constrain candidate `j`? Never for the owner project. For anyone else: while the window is
    active, or when `j` would still be running at the next window start. Restartable jobs without an estimate are
    exempt from the overlap rule: they can be unloaded when the window opens."""
    if j.project == r.project:
        return False
    if current(r, now_ms, cfg) is not None:
        return True
    est = j.estimate_s
    if est is None:
        if j.checkpoint == "restartable":
            return False
        est = cfg.default_unknown_estimate_s
    nxt = next_start(r, now_ms, cfg)
    return nxt is not None and now_ms + est * 1000 > nxt.start_ms


def applicable(j: JobView, now_ms: int, cfg: Config) -> list:
    return [r for r in parse(cfg) if applies(j, r, now_ms, cfg)]


def owner_window_active(j: JobView, now_ms: int, cfg: Config) -> bool:
    """Owner-project jobs treat their queue as `day` while their window is active (16 section 2)."""
    if not cfg.reservations:
        return False
    return any(r.project == j.project and current(r, now_ms, cfg) is not None for r in parse(cfg))


def _owner_gpu(r: Reservation, snap: Snapshot, ignoring: frozenset) -> tuple:
    """(owner holds any GPU slot, owner holds the exclusive GPU)."""
    held = [x for x in running(snap) if x.project == r.project and x.id not in ignoring]
    return any(x.gpu != "none" for x in held), any(x.gpu == "exclusive" for x in held)


def blocking(j: JobView, snap: Snapshot, cfg: Config, led, live_bytes: int, plain_reasons: list,
             ignoring: frozenset = frozenset()) -> list:
    """`reservation_window:<name>` codes for the reservations that block `j` although the plain checks did not.
    `live_bytes` is the live-free figure fits() already computed (reclaimable + add-back - pending - headroom)."""
    out = []
    for r in applicable(j, snap.now_ms, cfg):
        rr = reserve_remaining(r, snap, ignoring)
        hit = False
        if rr > 0:
            if "memory_ledger" not in plain_reasons and led.reserved_bytes + j.memory_bytes > snap.budget_bytes - rr:
                hit = True
            if "memory_live" not in plain_reasons and live_bytes - rr < j.memory_bytes:
                hit = True
        if j.gpu != "none" and r.gpu != "none" and "gpu" not in plain_reasons:
            any_gpu, excl = _owner_gpu(r, snap, ignoring)
            if r.gpu == "exclusive":
                hit = hit or not excl
            elif j.gpu == "exclusive":      # r.gpu == "shared": a slot is reserved, so no one may take it whole
                hit = hit or not any_gpu
            else:                           # j is shared: the reserved slot counts against gpu_shared_max
                hit = hit or (not any_gpu and led.gpu_shared + 1 >= cfg.gpu_shared_max)
        if hit:
            out.append(code(r.name))
    return out


def code(name: str) -> str:
    return f"reservation_window:{name}"


# ---- window start (phase 4b) ----------------------------------------------------------------------------------
def _virtual_target(r: Reservation, snap: Snapshot) -> JobView:
    """What the owner still needs at window start: the unreserved memory, and the GPU unless the owner already has it."""
    any_gpu, _ = _owner_gpu(r, snap, frozenset())
    return JobView(id=f"(reservation:{r.name})", project=r.project, state="queued", checkpoint="atomic",
                   gpu="none" if any_gpu else r.gpu, memory_bytes=reserve_remaining(r, snap),
                   enqueued_ms=snap.now_ms)


def plan(snap: Snapshot, cfg: Config) -> Plan:
    """At the start of each window (once per reservation per civil date) unload what blocks it: restartable
    daemon-spawned jobs of other projects, one checkpoint request for cooperative ones, nothing for atomic ones."""
    if not cfg.enforce or not cfg.reservations:
        return Plan.empty()
    from gsched.policy import preempt
    from gsched.policy.fit import fits
    p = Plan()
    for r in parse(cfg):
        occ = current(r, snap.now_ms, cfg)
        if occ is None or snap.reservation_latches.get(r.name) == occ.date:
            continue
        p.set_meta[LATCH_PREFIX + r.name] = occ.date
        if not r.unload_restartable:
            continue
        target = _virtual_target(r, snap)
        if target.memory_bytes == 0 and target.gpu == "none":
            continue
        victims = preempt.find_victims(target, snap, cfg, mode="window")
        by = {j.id: j for j in running(snap)}
        stops = [v for v in victims if by[v].checkpoint == "restartable"]
        ckpts = [v for v in victims if by[v].checkpoint != "restartable"]
        p.stop += [(v, "reservation") for v in stops]
        p.checkpoint += [(v, "reservation") for v in ckpts]
        if victims:
            p.freeze_admission = True
        ok, _ = fits(target, snap, cfg, ignoring=frozenset(victims))
        if not ok:
            stuck = sorted(j.id for j in running(snap)
                           if j.project != r.project and j.id not in victims
                           and (j.checkpoint == "atomic" or j.spilled or j.spawn != "daemon"))
            if stuck:
                p.events.append(("reservation_blocked_by_atomic", None,
                                 {"reservation": r.name, "jobs": stuck, "gib": round(target.memory_bytes / GiB, 2)}))
        p.events.append(("reservation_start", None, {"reservation": r.name, "date": occ.date,
                                                      "stop": stops, "checkpoint": ckpts}))
    return p
