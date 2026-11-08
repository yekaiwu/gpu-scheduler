"""Clock protocol. Tests substitute FakeClock and never sleep."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...  # wall clock, epoch ms

    def mono(self) -> float: ...  # monotonic seconds


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def mono(self) -> float:
        return time.monotonic()


class FakeClock:
    def __init__(self, start_ms: int):
        self._wall_ms = int(start_ms)
        self._mono = 1000.0

    def now_ms(self) -> int:
        return self._wall_ms

    def mono(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        """Move both clocks."""
        self._wall_ms += int(round(seconds * 1000))
        self._mono += seconds

    def set_wall(self, ms: int) -> None:
        """Simulate an NTP or sleep jump: only the wall clock moves."""
        self._wall_ms = int(ms)
