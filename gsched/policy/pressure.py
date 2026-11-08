"""Memory pressure state and the pressure ladder (06 section 10). Pure."""

from __future__ import annotations

from collections import deque
from typing import Literal

from gsched.config import Config
from gsched.policy.model import Plan, Snapshot, running
from gsched.units import GiB

ALARM_INTERVAL_MS = 5 * 60 * 1000


class SwapWindow:
    """60 s sliding window of (t_ms, swap_used_bytes). Data structure only: the daemon owns the instance."""

    def __init__(self, span_ms: int = 60_000):
        self.span_ms = span_ms
        self.buf: deque = deque()

    def add(self, t_ms: int, swap_used: int | None) -> None:
        if swap_used is None:
            return
        self.buf.append((t_ms, swap_used))
        while self.buf and t_ms - self.buf[0][0] > self.span_ms:
            self.buf.popleft()

    def rate_gib_per_min(self) -> float | None:
        if len(self.buf) < 2:
            return None
        (t0, v0), (t1, v1) = self.buf[0], self.buf[-1]
        if t1 - t0 < 5_000:
            return None
        return (v1 - v0) / GiB / ((t1 - t0) / 60_000)


def _trips(spec: dict, mem, rate: float | None) -> list[str]:
    out = []
    lb = spec.get("level_below")
    if lb is not None and mem.level is not None and mem.level < lb:
        out.append(f"level<{lb}")
    sr = spec.get("swap_rise_gib_per_min")
    if sr is not None and rate is not None and rate >= sr:
        out.append(f"swap_rise>={sr}")
    rb = spec.get("reclaimable_below_gib")
    if rb is not None and mem.reclaimable_bytes < rb * GiB:
        out.append(f"reclaimable<{rb}GiB")
    return out


def level(mem, swap_rate: float | None, cfg: Config) -> tuple[Literal["normal", "warn", "critical"], list[str]]:
    """Any single condition trips; critical is checked first. Unreadable swap fails closed to warn."""
    c = _trips(cfg.pressure["critical"], mem, swap_rate)
    if c:
        return "critical", c
    w = _trips(cfg.pressure["warn"], mem, swap_rate)
    if mem.swap_used_bytes is None:
        w.append("swap_unknown")
    if w:
        return "warn", w
    return "normal", []


def victims_for_pressure(snap: Snapshot, cfg: Config):
    own = [j for j in running(snap) if j.spawn == "daemon" and j.held_reason == "live" and not j.spilled]
    order = lambda v: (v.priority, -v.project_usage / max(v.project_weight, 1), -(v.started_ms or 0))
    restartables = sorted((j for j in own if j.checkpoint == "restartable"), key=order)
    if restartables:
        return "stop", restartables[0]
    if cfg.pressure_checkpoint_cooperative:
        coops = sorted((j for j in own if j.checkpoint == "cooperative"), key=order)
        if coops:
            return "checkpoint", coops[0]
    return None, None


def plan(snap: Snapshot, cfg: Config) -> Plan:
    p = Plan()
    if snap.pressure != "normal":
        p.freeze_admission = True
    if snap.pressure == "critical" and cfg.enforce and (
            snap.last_pressure_action_ms is None
            or snap.now_ms - snap.last_pressure_action_ms >= cfg.pressure_step_s * 1000):
        what, v = victims_for_pressure(snap, cfg)
        if v is not None:
            (p.stop if what == "stop" else p.checkpoint).append((v.id, "pressure"))
            p.set_meta["last_pressure_action_ms"] = str(snap.now_ms)
        elif snap.last_alarm_ms is None or snap.now_ms - snap.last_alarm_ms >= ALARM_INTERVAL_MS:
            p.alarm.append("pressure_critical_no_action")
    if snap.protective and cfg.protective_unload_restartable and cfg.enforce:
        for j in running(snap):
            if j.checkpoint == "restartable" and j.spawn == "daemon" and j.gpu != "none" and j.held_reason == "live":
                p.stop.append((j.id, "protective"))
    return p
