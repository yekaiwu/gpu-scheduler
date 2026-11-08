"""Config dataclass, load, validate (01 section 7)."""

from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable

from gsched.errors import ConfigError

DEFAULT_PRESSURE = {
    "warn": {"level_below": 30, "swap_rise_gib_per_min": 1.0, "reclaimable_below_gib": 4},
    "critical": {"level_below": 15, "swap_rise_gib_per_min": 4.0, "reclaimable_below_gib": 1.5},
}
DEFAULT_NIGHT_WINDOW = {"start": "23:00", "end": "08:00"}
DEFAULT_BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
RESERVATION_NAME = re.compile(r"^[a-z0-9-]{1,40}$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RESERVATION_KEYS = {"name", "days", "start", "end", "project", "memory_gib", "gpu", "unload_restartable"}
YIELD_APP_KEYS = {"name", "app", "reservations", "for_jobs_gib", "job_wait_s", "busy_ports", "quit", "quit_timeout_s",
                  "reopen", "reopen_delay_s"}
YIELD_APP_DEFAULTS = {"reservations": [], "for_jobs_gib": None, "job_wait_s": 60, "busy_ports": [],
                      "quit": "applescript", "quit_timeout_s": 60, "reopen": True, "reopen_delay_s": 60}


@dataclass(frozen=True)
class Config:
    enforce: bool = False
    timezone: str | None = None
    night_window: dict | None = field(default_factory=lambda: dict(DEFAULT_NIGHT_WINDOW))
    day_max_s: int = 7200
    backfill: bool = False
    morning_unload_restartable: bool = True
    allow_spill: bool = False
    on_timeout: str = "wait"
    grace_s: int = 120
    kill_grace_s: int = 15
    coop_min_hold_s: int = 300
    restartable_min_hold_s: int = 60
    preempt_restartable: bool = False
    preempt_grace_ms: int = 180000
    fair_halflife_s: int = 21600
    starve_s: int = 14400
    os_reserve_gib: float | None = None
    headroom_gib: float = 2
    gpu_shared_max: int = 2
    cpu_budget: int | None = None
    footprint_note_gib: float = 2
    heavy_basenames: tuple = ("llama-server", "vllm", "ollama")
    heavy_listen_ports: tuple = ()
    port_probe_s: int = 5
    protective_unload_restartable: bool = True
    protective_clear_s: int = 30
    pressure: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_PRESSURE))
    pressure_checkpoint_cooperative: bool = False
    pressure_step_s: int = 10
    checkpoint_signal: str = "SIGUSR1"
    attach_grace_ms: int = 30000
    spawn_report_timeout_ms: int = 10000
    prevent_sleep: bool = True
    base_path: str = DEFAULT_BASE_PATH
    socket_path: str | None = None
    log_max_bytes: int = 5000000
    retention_days: int = 30
    mcp_max_wait_s: int = 30
    gpu_busy_foreign_pct: int | None = None
    launchd_label: str = "com.gpuscheduler.agent"
    default_project: str | None = None
    reservations: tuple = ()
    default_unknown_estimate_s: int = 7200
    job_log_max_bytes: int = 1073741824
    job_log_keep_bytes: int = 67108864
    tcc_protected_roots: tuple = ()
    yield_apps: tuple = ()

    # ---- construction -------------------------------------------------
    @classmethod
    def defaults(cls) -> "Config":
        return cls()

    def replace(self, **kw) -> "Config":
        """Test helper: replace fields, then re-validate."""
        c = replace(self, **kw)
        c._validate()
        return c

    @classmethod
    def from_dict(cls, raw: dict, on_unknown: Callable[[str], None] | None = None,
                  seen_unknown: set | None = None) -> "Config":
        if not isinstance(raw, dict):
            raise ConfigError("<root>", "config must be a JSON object")
        known = {f.name for f in fields(cls)}
        kw: dict[str, Any] = {}
        for k, v in raw.items():
            if k.startswith("_"):
                continue  # _comment etc.
            if k not in known:
                if seen_unknown is None or k not in seen_unknown:
                    if seen_unknown is not None:
                        seen_unknown.add(k)
                    if on_unknown:
                        on_unknown(k)
                continue
            kw[k] = v
        # normalise lists
        for k in ("heavy_basenames", "heavy_listen_ports", "reservations", "tcc_protected_roots", "yield_apps"):
            if k in kw:
                if not isinstance(kw[k], (list, tuple)):
                    raise ConfigError(k, "must be a list")
                kw[k] = (tuple(copy.deepcopy(x) for x in kw[k]) if k in ("reservations", "yield_apps")
                         else tuple(kw[k]))
        if "pressure" in kw:
            kw["pressure"] = _merge_pressure(kw["pressure"])
        c = cls(**kw)
        c._validate()
        return c

    @classmethod
    def load(cls, path: str | os.PathLike, on_unknown: Callable[[str], None] | None = None,
             seen_unknown: set | None = None) -> "Config":
        """A missing file means all defaults (which includes enforce=false)."""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return cls.defaults()
        try:
            raw = json.loads(data, parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError) as e:
            raise ConfigError("<file>", f"{path} is not valid JSON: {e}")
        return cls.from_dict(raw, on_unknown, seen_unknown)

    def as_dict(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, tuple):
                v = [copy.deepcopy(x) for x in v]
            elif isinstance(v, dict):
                v = copy.deepcopy(v)
            d[f.name] = v
        return d

    # ---- validation ---------------------------------------------------
    def _validate(self) -> None:
        def need(cond: bool, key: str, msg: str) -> None:
            if not cond:
                raise ConfigError(key, msg)

        def is_bool(k):
            need(isinstance(getattr(self, k), bool), k, "must be a boolean")

        def is_int(k, lo=None, hi=None, nullable=False):
            v = getattr(self, k)
            if v is None and nullable:
                return
            need(isinstance(v, int) and not isinstance(v, bool), k, "must be an integer")
            if lo is not None:
                need(v >= lo, k, f"must be >= {lo}")
            if hi is not None:
                need(v <= hi, k, f"must be <= {hi}")

        def is_num(k, lo=None, nullable=False, strict_lo=False):
            v = getattr(self, k)
            if v is None and nullable:
                return
            need(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v), k, "must be a number")
            if lo is not None:
                need(v > lo if strict_lo else v >= lo, k, f"must be {'>' if strict_lo else '>='} {lo}")

        for k in ("enforce", "backfill", "morning_unload_restartable", "allow_spill", "preempt_restartable",
                  "protective_unload_restartable", "pressure_checkpoint_cooperative", "prevent_sleep"):
            is_bool(k)
        if self.timezone is not None:
            need(isinstance(self.timezone, str), "timezone", "must be a string or null")
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(self.timezone)
            except Exception:
                raise ConfigError("timezone", f"unknown IANA timezone {self.timezone!r}")
        nw = self.night_window
        if nw is not None:
            need(isinstance(nw, dict) and set(nw) == {"start", "end"}, "night_window",
                 'must be null or {"start":"HH:MM","end":"HH:MM"}')
            for kk in ("start", "end"):
                need(isinstance(nw[kk], str) and _HHMM.match(nw[kk]) is not None, "night_window",
                     f"{kk} must be HH:MM")
            need(nw["start"] != nw["end"], "night_window", "start must differ from end")
        is_int("day_max_s", 1)
        need(self.on_timeout in ("wait", "kill"), "on_timeout", "must be 'wait' or 'kill'")
        is_int("grace_s", 1, 86400)
        is_int("kill_grace_s", 1, 300)
        is_int("coop_min_hold_s", 0)
        is_int("restartable_min_hold_s", 0)
        is_int("preempt_grace_ms", 0)
        is_int("fair_halflife_s", 1)
        is_int("starve_s", 0)
        is_num("os_reserve_gib", 0, nullable=True)
        is_num("headroom_gib", 0)
        is_int("gpu_shared_max", 1)
        is_int("cpu_budget", 1, nullable=True)
        is_num("footprint_note_gib", 0)
        need(all(isinstance(x, str) and x and "/" not in x for x in self.heavy_basenames), "heavy_basenames",
             "must be a list of executable basenames")
        need(all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 65535 for x in self.heavy_listen_ports),
             "heavy_listen_ports", "ports must be integers 1..65535")
        is_int("port_probe_s", 1)
        is_int("protective_clear_s", 0)
        is_int("pressure_step_s", 0)
        need(self.checkpoint_signal in ("SIGUSR1", "SIGUSR2"), "checkpoint_signal", "must be SIGUSR1 or SIGUSR2")
        is_int("attach_grace_ms", 0)
        is_int("spawn_report_timeout_ms", 1)
        need(isinstance(self.base_path, str) and self.base_path, "base_path", "must be a non-empty string")
        need(self.socket_path is None or (isinstance(self.socket_path, str) and self.socket_path),
             "socket_path", "must be a path string or null")
        is_int("log_max_bytes", 1024)
        is_int("retention_days", 1)
        is_int("mcp_max_wait_s", 0, 300)
        is_int("gpu_busy_foreign_pct", 1, 100, nullable=True)
        need(isinstance(self.launchd_label, str) and re.match(r"^[A-Za-z0-9._-]+$", self.launchd_label) is not None,
             "launchd_label", "must be a label string")
        need(self.default_project is None or isinstance(self.default_project, str), "default_project",
             "must be a string or null")
        is_int("default_unknown_estimate_s", 1)
        is_int("job_log_max_bytes", 1024)
        is_int("job_log_keep_bytes", 0)
        need(self.job_log_keep_bytes <= self.job_log_max_bytes, "job_log_keep_bytes",
             "must be <= job_log_max_bytes")
        need(all(isinstance(x, str) and os.path.isabs(os.path.expanduser(x)) for x in self.tcc_protected_roots),
             "tcc_protected_roots", "must be a list of absolute directory paths (~ allowed)")
        self._validate_reservations()
        self._validate_yield_apps()
        # pressure
        p = self.pressure
        need(isinstance(p, dict) and set(p) == {"warn", "critical"}, "pressure", "must have warn and critical objects")
        for lvl in ("warn", "critical"):
            o = p[lvl]
            need(isinstance(o, dict) and set(o) == {"level_below", "swap_rise_gib_per_min", "reclaimable_below_gib"},
                 f"pressure.{lvl}", "needs level_below, swap_rise_gib_per_min, reclaimable_below_gib")
            for kk, vv in o.items():
                need(vv is None or (isinstance(vv, (int, float)) and not isinstance(vv, bool) and math.isfinite(vv)
                                    and vv >= 0), f"pressure.{lvl}.{kk}", "must be a non-negative number or null")

    def _validate_reservations(self) -> None:
        rs = self.reservations
        if not isinstance(rs, (list, tuple)):
            raise ConfigError("reservations", "must be a list")
        seen: set = set()
        for i, r in enumerate(rs):
            key = f"reservations[{i}]"

            def need(cond: bool, sub: str, msg: str) -> None:
                if not cond:
                    raise ConfigError(f"{key}.{sub}" if sub else key, msg)

            need(isinstance(r, dict), "", "must be an object")
            unknown = sorted(set(r) - RESERVATION_KEYS)
            need(not unknown, unknown[0] if unknown else "", f"unknown key(s): {', '.join(unknown)}")
            for req in ("name", "days", "start", "end", "project", "memory_gib"):
                need(req in r, req, "is required")
            need(isinstance(r["name"], str) and RESERVATION_NAME.match(r["name"]) is not None, "name",
                 "must match ^[a-z0-9-]{1,40}$")
            need(r["name"] not in seen, "name", f"duplicate reservation name {r['name']!r}")
            seen.add(r["name"])
            d = r["days"]
            need(isinstance(d, (list, tuple)) and len(d) > 0 and all(isinstance(x, str) for x in d), "days",
                 'must be a non-empty list of three-letter lowercase weekdays, or ["daily"]')
            need(all(x == "daily" or x in WEEKDAYS for x in d), "days",
                 'entries must be mon..sun (lowercase, three letters) or "daily"')
            for kk in ("start", "end"):
                need(isinstance(r[kk], str) and _HHMM.match(r[kk]) is not None, kk, "must be HH:MM")
            need(isinstance(r["project"], str) and r["project"] != "", "project", "must be a project name")
            m = r["memory_gib"]
            need(isinstance(m, (int, float)) and not isinstance(m, bool) and math.isfinite(m) and m > 0, "memory_gib",
                 "must be a number > 0")
            need(r.get("gpu", "none") in ("exclusive", "shared", "none"), "gpu", "must be exclusive, shared or none")
            need(isinstance(r.get("unload_restartable", False), bool), "unload_restartable", "must be a boolean")

    def _validate_yield_apps(self) -> None:
        """`yield_apps` (QUESTIONS #41): desktop apps the owner lets the daemon quit while their memory is needed."""
        ya = self.yield_apps
        if not isinstance(ya, (list, tuple)):
            raise ConfigError("yield_apps", "must be a list")
        res_names = {r.get("name") for r in self.reservations if isinstance(r, dict)}
        seen: set = set()
        for i, a in enumerate(ya):
            key = f"yield_apps[{i}]"

            def need(cond: bool, sub: str, msg: str) -> None:
                if not cond:
                    raise ConfigError(f"{key}.{sub}" if sub else key, msg)

            def nat(sub: str, lo: int, hi: int) -> None:
                v = a.get(sub, YIELD_APP_DEFAULTS[sub])
                need(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi, sub,
                     f"must be an integer from {lo} to {hi}")

            need(isinstance(a, dict), "", "must be an object")
            unknown = sorted(set(a) - YIELD_APP_KEYS)
            need(not unknown, unknown[0] if unknown else "", f"unknown key(s): {', '.join(unknown)}")
            for req in ("name", "app"):
                need(req in a, req, "is required")
            need(isinstance(a["name"], str) and RESERVATION_NAME.match(a["name"]) is not None, "name",
                 "must match ^[a-z0-9-]{1,40}$")
            need(a["name"] not in seen, "name", f"duplicate yield app name {a['name']!r}")
            seen.add(a["name"])
            app = a["app"]
            need(isinstance(app, str) and os.path.isabs(os.path.expanduser(app)) and app.rstrip("/").endswith(".app"),
                 "app", "must be the absolute path of a .app bundle")
            rs = a.get("reservations", [])
            need(isinstance(rs, (list, tuple)) and all(isinstance(x, str) for x in rs), "reservations",
                 "must be a list of reservation names")
            for x in rs:
                need(x in res_names, "reservations", f"no reservation named {x!r}")
            fj = a.get("for_jobs_gib")
            need(fj is None or (isinstance(fj, (int, float)) and not isinstance(fj, bool) and math.isfinite(fj)
                                and fj > 0), "for_jobs_gib", "must be null or a number > 0")
            need(bool(rs) or fj is not None, "", "needs reservations or for_jobs_gib (otherwise it never yields)")
            nat("job_wait_s", 0, 86400)
            nat("quit_timeout_s", 5, 600)
            nat("reopen_delay_s", 0, 86400)
            bp = a.get("busy_ports", [])
            need(isinstance(bp, (list, tuple)) and all(isinstance(x, int) and not isinstance(x, bool) and 0 < x < 65536
                                                       for x in bp), "busy_ports", "must be a list of TCP ports")
            need(a.get("quit", "applescript") in ("applescript", "sigterm"), "quit", "must be applescript or sigterm")
            need(isinstance(a.get("reopen", True), bool), "reopen", "must be a boolean")


def system_timezone_name() -> str:
    """The machine's civil time zone (IANA name) from /etc/localtime; falls back to time.tzname, then UTC."""
    from zoneinfo import ZoneInfo
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            ZoneInfo(name)
            return name
    except Exception:
        pass
    import time
    for name in time.tzname:
        try:
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return "UTC"


def _merge_pressure(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("pressure", "must be an object")
    out = copy.deepcopy(DEFAULT_PRESSURE)
    for lvl, o in raw.items():
        if lvl not in out:
            raise ConfigError("pressure", f"unknown level {lvl!r}")
        if not isinstance(o, dict):
            raise ConfigError(f"pressure.{lvl}", "must be an object")
        for kk, vv in o.items():
            if kk not in out[lvl]:
                raise ConfigError(f"pressure.{lvl}", f"unknown key {kk!r}")
            out[lvl][kk] = vv
    return out


def _reject_constant(name: str):
    raise ValueError(f"invalid JSON constant {name}")


def write_config_atomic(path: str | os.PathLike, raw: dict) -> None:
    """Write config JSON atomically with mode 0600."""
    path = os.fspath(path)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(raw, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)
