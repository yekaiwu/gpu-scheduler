"""History-based suggestions and submit-time warnings (12 section 6). Rows are written by store.py."""

from __future__ import annotations

import math

from gsched.units import GiB


def _p(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))
    return s[k]


def suggest(store, project: str, name: str | None) -> dict:
    rows = store.history_rows(project, name, 50)
    n = len(rows)
    if n < 3:
        return {"n": n, "note": "not enough history"}
    peaks = [r["peak_footprint_bytes"] for r in rows if r["peak_footprint_bytes"]]
    rts = [r["runtime_s"] for r in rows if r["runtime_s"] is not None]
    out: dict = {"n": n}
    if peaks:
        p95 = _p(peaks, 0.95)
        out["memory_gib_p95"] = math.ceil(p95 * 1.10 / GiB * 2) / 2
        out["peak_gib_max"] = round(max(peaks) / GiB, 2)
    else:
        out["memory_gib_p95"] = None
        out["peak_gib_max"] = None
    out["runtime_s_p50"] = _p(rts, 0.5) if rts else None
    out["runtime_s_p95"] = _p(rts, 0.95) if rts else None
    return out


def _dur(s: float) -> str:
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s % 60:02d}s"


def submit_warnings(store, spec, machine) -> list[str]:
    warns: list[str] = []
    sg = suggest(store, spec.project, spec.name)
    if sg.get("n", 0) >= 3:
        p95_peak = None
        rows = store.history_rows(spec.project, spec.name, 50)
        peaks = [r["peak_footprint_bytes"] for r in rows if r["peak_footprint_bytes"]]
        if peaks:
            p95_peak = _p(peaks, 0.95)
        if p95_peak and spec.memory_bytes < p95_peak:
            warns.append(f"memory_gib {spec.memory_bytes / GiB:g} is below the p95 peak of {p95_peak / GiB:.1f} GiB "
                         f"for {spec.project}/{spec.name or '*'}")
        p50 = sg.get("runtime_s_p50")
        if p50 and spec.estimate_s is not None and spec.estimate_s < p50 * 0.5:
            warns.append(f"estimate looks short (p50 {_dur(p50)})")
    if spec.gpu == "exclusive" and machine.gpu_wired_limit_mb == 0 \
            and spec.memory_bytes >= 0.75 * machine.memsize_bytes:
        warns.append("may exceed the default Metal wired-memory limit; see `gsched doctor`")
    return warns
