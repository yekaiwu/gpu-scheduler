"""Fair share, decay and ranking (06 section 4)."""

from __future__ import annotations

from gsched.config import Config
from gsched.policy.model import Snapshot


def share(j, snap: Snapshot) -> float:
    g = {"exclusive": 1.0, "shared": 0.5, "none": 0.0}[j.gpu]
    m = j.memory_bytes / snap.budget_bytes if snap.budget_bytes else 1.0
    c = j.cpu_cores / snap.cpu_budget if snap.cpu_budget else 1.0
    return min(1.0, max(g, m, c))


def decay(usage: float, dt_s: float, halflife_s: int) -> float:
    return usage * 0.5 ** (dt_s / halflife_s)


def clamp_dt(dt_s: float) -> float:
    return max(0.0, min(3600.0, dt_s))


def charge(prev: dict, last_ms: dict, now_ms: int, snap: Snapshot, cfg: Config) -> dict:
    """New usage per project = decay(old, dt) + sum(share(j) * dt) for its running jobs whose gate was released."""
    out = {}
    projects = set(prev) | {j.project for j in snap.jobs}
    for p in projects:
        dt_s = clamp_dt((now_ms - last_ms.get(p, now_ms)) / 1000)
        u = decay(prev.get(p, 0.0), dt_s, cfg.fair_halflife_s)
        for j in snap.jobs:
            if j.project == p and j.state == "held" and j.started_ms is not None:
                u += share(j, snap) * dt_s
        out[p] = u
    return out


def rank_key(j, snap: Snapshot | None = None):
    return (-j.priority, j.project_usage / max(j.project_weight, 1), j.enqueued_ms, j.id)
