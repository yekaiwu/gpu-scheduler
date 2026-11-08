"""Snapshot builder for pure policy tests (13 section 2)."""

from __future__ import annotations

import datetime as dt

from gsched.config import Config
from gsched.platform import MachineInfo, MemSample
from gsched.policy.fit import compute_budget, compute_cpu_budget
from gsched.policy.model import AbortLines, Baseline, JobView, Snapshot
from gsched.units import GiB, gib_to_bytes


def ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class SnapBuilder:
    def __init__(self, memsize_gib=48, reclaimable_gib=30, now="2026-11-08T12:00:00Z", cpu=18, level=80,
                 swap_used_gib=0.0, swap_known=True, pressure="normal", protective=False, draining=False,
                 morning_latch=None, page=16384):
        self.memsize_gib, self.reclaimable_gib, self.cpu = memsize_gib, reclaimable_gib, cpu
        self.now_ms = ms(now)
        self.level, self.swap_used_gib, self.swap_known = level, swap_used_gib, swap_known
        self.pressure, self.protective, self.draining, self.morning_latch = pressure, protective, draining, morning_latch
        self.page = page
        self.jobs: list[JobView] = []
        self.projects: dict[str, tuple[float, int]] = {}
        self.dep_states: dict[str, str] = {}
        self.baselines: dict = {}
        self.last_pressure_action_ms = None
        self.last_alarm_ms = None
        self.reservation_latches: dict = {}

    def project(self, name, usage=0.0, weight=1):
        self.projects[name] = (usage, weight)
        return self

    def dep(self, job_id, state):
        self.dep_states[job_id] = state
        return self

    def _t(self, v):
        """seconds-ago (number) or absolute ms."""
        return None if v is None else int(self.now_ms - v * 1000)

    def _add(self, **kw):
        proj = kw.setdefault("project", "p")
        usage, weight = self.projects.get(proj, (0.0, 1))
        kw.setdefault("project_usage", usage)
        kw.setdefault("project_weight", weight)
        mem = kw.pop("mem", 1)
        kw["memory_bytes"] = gib_to_bytes(mem)
        for k in ("footprint_gib", "ceiling_gib"):
            if k in kw:
                v = kw.pop(k)
                kw["footprint_bytes" if k == "footprint_gib" else "ceiling_bytes"] = None if v is None else gib_to_bytes(v)
        if "locks" in kw:
            kw["locks"] = frozenset(kw["locks"])
        if "after" in kw:
            kw["after"] = tuple(kw["after"])
        self.jobs.append(JobView(**kw))
        return self

    def running(self, id, gpu="none", mem=1, checkpoint="atomic", priority=0, started_ago=600, queue="day",
                held_reason="live", **kw):
        started = self._t(started_ago)
        kw.setdefault("held_ms", started)
        kw.setdefault("enqueued_ms", started)
        kw.setdefault("footprint_gib", mem)
        kw.setdefault("ready", True)
        return self._add(id=id, state="held", held_reason=held_reason, gpu=gpu, mem=mem, checkpoint=checkpoint,
                         priority=priority, started_ms=started, queue=queue, **kw)

    def queued(self, id, gpu="none", mem=1, checkpoint="atomic", priority=0, queue="day", waited=60, **kw):
        kw.setdefault("enqueued_ms", self._t(waited))
        kw.setdefault("estimate_s", 60)
        kw.setdefault("state", "queued")
        return self._add(id=id, gpu=gpu, mem=mem, checkpoint=checkpoint, priority=priority,
                         queue=queue, **kw)

    def build(self, cfg: Config | None = None) -> Snapshot:
        cfg = cfg or Config.defaults().replace(enforce=True, timezone="UTC")
        machine = MachineInfo(gib_to_bytes(self.memsize_gib), self.page, self.cpu, None, None, "Fake", "27.0", 0)
        pages = gib_to_bytes(self.reclaimable_gib) // self.page
        mem = MemSample(self.now_ms, pages, 0, 0, 0, 0, 0, self.page, self.level,
                        gib_to_bytes(self.swap_used_gib) if self.swap_known else None,
                        gib_to_bytes(4) if self.swap_known else None)
        return Snapshot(
            now_ms=self.now_ms, machine=machine, mem=mem, budget_bytes=compute_budget(machine, cfg),
            cpu_budget=compute_cpu_budget(machine, cfg), jobs=tuple(self.jobs), dep_states=dict(self.dep_states),
            protective=self.protective, pressure=self.pressure, morning_latch=self.morning_latch,
            draining=self.draining, last_pressure_action_ms=self.last_pressure_action_ms,
            baselines=dict(self.baselines), last_alarm_ms=self.last_alarm_ms,
            reservation_latches=dict(self.reservation_latches))


def cfg(**kw) -> Config:
    base = dict(enforce=True, timezone="UTC")
    base.update(kw)
    return Config.defaults().replace(**base)
