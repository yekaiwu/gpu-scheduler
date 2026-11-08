"""Rotating decision log (01 section 8). Every decision line is also an event row (via the sink)."""

from __future__ import annotations

import datetime
import os
from typing import Callable

from gsched.clock import Clock
from gsched.units import GiB


def _fmt_value(k: str, v) -> tuple[str, str]:
    if k.endswith("_bytes") and isinstance(v, (int, float)) and not isinstance(v, bool):
        return k[:-6], f"{v / GiB:.1f}GiB"
    if isinstance(v, bool):
        return k, "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return k, "[" + ",".join(str(x) for x in v) + "]"
    if v is None:
        return k, "null"
    s = str(v)
    if s == "" or any(c in s for c in " \t\n\"="):
        s = '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return k, s


class DecisionLog:
    def __init__(self, path: str | None, max_bytes: int, clock: Clock,
                 sink: Callable[[str, str | None, dict], None] | None = None, enabled: bool = True):
        self.path = path
        self.max_bytes = max_bytes
        self.clock = clock
        self.sink = sink
        self.enabled = enabled
        self._fh = None
        self._size = 0

    def set_max_bytes(self, n: int) -> None:
        self.max_bytes = n

    def _open(self) -> None:
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._fh = os.fdopen(fd, "a", encoding="utf-8")
        try:
            self._size = os.fstat(fd).st_size
        except OSError:
            self._size = 0

    def _rotate(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        for i in (2, 1):
            src = f"{self.path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{self.path}.{i + 1}")
        if os.path.exists(self.path):
            os.replace(self.path, f"{self.path}.1")

    def format_line(self, kind: str, fields: dict) -> str:
        ts = datetime.datetime.fromtimestamp(self.clock.now_ms() / 1000).astimezone().isoformat(timespec="milliseconds")
        parts = [ts, kind]
        for k, v in fields.items():
            kk, vv = _fmt_value(k, v)
            parts.append(f"{kk}={vv}")
        return " ".join(parts)

    def decision(self, kind: str, **fields) -> None:
        """Write one log line and one event row. `job` (if present) is the event's job column."""
        if self.sink is not None:
            body = {k: v for k, v in fields.items() if k != "job"}
            try:
                self.sink(kind, fields.get("job"), body)
            except Exception:  # the log must never take the daemon down
                pass
        if not self.enabled or not self.path:
            return
        try:
            if self._fh is None:
                self._open()
            line = self.format_line(kind, fields) + "\n"
            if self._size >= self.max_bytes:
                self._rotate()
                self._open()
            self._fh.write(line)
            self._fh.flush()
            self._size += len(line.encode("utf-8"))
        except OSError:
            pass

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
