"""Night window and civil-time helpers (06 section 3). The only place civil time is computed."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from gsched.config import Config


def tz(cfg: Config) -> ZoneInfo:
    """cfg.timezone, or UTC when unset. The daemon resolves the machine's civil time zone into cfg.timezone at load
    time (config.system_timezone_name), so nothing here reads the system."""
    return ZoneInfo(cfg.timezone) if cfg.timezone else ZoneInfo("UTC")


def _hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def _local(now_ms: int, cfg: Config) -> dt.datetime:
    return dt.datetime.fromtimestamp(now_ms / 1000, tz=tz(cfg))


def in_night(now_ms: int, cfg: Config) -> bool:
    nw = cfg.night_window
    if nw is None:
        return False
    t = _local(now_ms, cfg).time()
    s, e = _hhmm(nw["start"]), _hhmm(nw["end"])
    return (s <= t < e) if s < e else (t >= s or t < e)


def _at(date: dt.date, t: dt.time, cfg: Config) -> int:
    return int(dt.datetime.combine(date, t, tzinfo=tz(cfg)).timestamp() * 1000)


def next_window_end_ms(now_ms: int, cfg: Config) -> int:
    """The next civil `end` strictly after now."""
    e = _hhmm(cfg.night_window["end"])
    today = _local(now_ms, cfg).date()
    cand = _at(today, e, cfg)
    if cand <= now_ms:
        cand = _at(today + dt.timedelta(days=1), e, cfg)
    return cand


def next_window_start_ms(now_ms: int, cfg: Config) -> int:
    s = _hhmm(cfg.night_window["start"])
    today = _local(now_ms, cfg).date()
    cand = _at(today, s, cfg)
    if cand <= now_ms:
        cand = _at(today + dt.timedelta(days=1), s, cfg)
    return cand


def past_end_today(now_ms: int, cfg: Config) -> bool:
    """Local wall time is at or after today's night_window.end."""
    return _local(now_ms, cfg).time() >= _hhmm(cfg.night_window["end"])


def civil_date(now_ms: int, cfg: Config) -> str:
    return _local(now_ms, cfg).date().isoformat()


def queue_open(j, now_ms: int, cfg: Config, day_waiting: bool) -> bool:
    if cfg.night_window is None or j.queue == "day":
        return True
    if in_night(now_ms, cfg):
        return True
    return cfg.backfill and not day_waiting


def atomic_fits(j, now_ms: int, cfg: Config) -> bool:
    if cfg.night_window is None or j.queue != "night":
        return True
    if j.checkpoint != "atomic" or j.allow_spill:
        return True
    if j.estimate_s is None:
        return False
    return now_ms + j.estimate_s * 1000 <= next_window_end_ms(now_ms, cfg)


# ---- reservation windows (16 section 2) -------------------------------------------------------------------------
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def window_occurrences(days, start: str, end: str, now_ms: int, cfg: Config, *, back_days: int = 1,
                       forward_days: int = 9) -> list[tuple[int, int, str]]:
    """Occurrences of a weekly window as (start_ms, end_ms, start_date), sorted by start. `days` lists the weekdays
    a window STARTS on (or "daily"); if end <= start the window wraps midnight and ends on the next civil date."""
    s, e = _hhmm(start), _hhmm(end)
    wraps = e <= s
    today = _local(now_ms, cfg).date()
    every = "daily" in days
    out = []
    for off in range(-back_days, forward_days + 1):
        d = today + dt.timedelta(days=off)
        if not every and _WEEKDAYS[d.weekday()] not in days:
            continue
        out.append((_at(d, s, cfg), _at(d + dt.timedelta(days=1) if wraps else d, e, cfg), d.isoformat()))
    return out
