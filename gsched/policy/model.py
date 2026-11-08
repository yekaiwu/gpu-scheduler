"""Frozen models shared by the policy planners (06 section 0)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

from gsched.platform import MachineInfo, MemSample


@dataclass(frozen=True, slots=True)
class AbortLines:
    level_below: int | None = None
    reclaimable_below_gib: float | None = None
    swap_rise_gib: float | None = None
    ceiling_gib: float | None = None

    @property
    def armed(self) -> bool:
        return any(v is not None for v in (self.level_below, self.reclaimable_below_gib, self.swap_rise_gib,
                                           self.ceiling_gib))


@dataclass(frozen=True, slots=True)
class Baseline:
    """Post-ready sample for the abort lines (08 section 7)."""
    swap_used_bytes: int | None
    level: int | None
    reclaimable_bytes: int


@dataclass(frozen=True, slots=True)
class JobView:
    id: str
    project: str
    state: str
    checkpoint: str
    gpu: str
    memory_bytes: int
    enqueued_ms: int
    name: str | None = None
    profile: str | None = None
    held_reason: str | None = None
    queue: str | None = "day"
    priority: int = 0
    spawn: str = "daemon"
    cpu_cores: int = 1
    locks: frozenset = frozenset()
    estimate_s: int | None = None
    limit_s: int | None = None
    allow_spill: bool = False
    after: tuple = ()
    level_min: int | None = None
    ceiling_bytes: int | None = None
    eligible_since_ms: int | None = None
    held_ms: int | None = None
    started_ms: int | None = None
    stop_requested_ms: int | None = None
    stop_reason: str | None = None
    spilled: bool = False
    checkpoint_failed: bool = False
    cancel_requested: bool = False
    hold: bool = False
    last_touch_ms: int | None = None
    switch_to: str | None = None
    footprint_bytes: int | None = None
    grace_s: int = 120
    on_timeout: str = "wait"
    start_waiter_parked: bool = False
    inhibited: bool = False
    project_present: bool = True
    project_weight: int = 1
    project_usage: float = 0.0
    # additions beyond the doc's minimum
    attempt: int = 1
    kill_sent: bool = False
    footprint_fail_n: int = 0
    estimate_overrun: bool = False
    over_limit: bool = False
    yield_requested: bool = False
    ready: bool = False
    ceiling_noted: bool = False
    idle_unload_s: int | None = None
    abort: AbortLines | None = None
    listen_port: int | None = None
    pid_present: bool = True
    abort_unavail_since_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    now_ms: int
    machine: MachineInfo
    mem: MemSample
    budget_bytes: int
    cpu_budget: int
    jobs: tuple
    dep_states: Mapping[str, str] = field(default_factory=dict)
    protective: bool = False
    pressure: Literal["normal", "warn", "critical"] = "normal"
    morning_latch: str | None = None
    draining: bool = False
    last_pressure_action_ms: int | None = None
    baselines: Mapping[str, Baseline] = field(default_factory=dict)
    swap_rise_gib_per_min: float | None = None
    last_alarm_ms: int | None = None
    reservation_latches: Mapping[str, str] = field(default_factory=dict)   # reservation name -> civil date handled


def running(snap: Snapshot) -> list[JobView]:
    return [j for j in snap.jobs if j.state == "held"]


@dataclass
class Plan:
    admit: str | None = None
    stop: list = field(default_factory=list)          # (job_id, reason): unload / begin_stop
    checkpoint: list = field(default_factory=list)    # (job_id, reason): one checkpoint request
    kill: list = field(default_factory=list)          # job ids: SIGKILL escalation
    spill: list = field(default_factory=list)         # (job_id, reason)
    kill_stop: list = field(default_factory=list)     # (job_id, reason): on_timeout=kill after grace
    flags: list = field(default_factory=list)         # (job_id, {col: value})
    events: list = field(default_factory=list)        # (kind, job_id|None, body)
    set_meta: dict = field(default_factory=dict)
    freeze_admission: bool = False
    reserve: str | None = None
    alarm: list = field(default_factory=list)
    blocked: dict = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "Plan":
        return cls()

    def is_empty(self) -> bool:
        return not any((self.admit, self.stop, self.checkpoint, self.kill, self.spill, self.kill_stop, self.flags,
                        self.events, self.set_meta, self.freeze_admission, self.alarm))
