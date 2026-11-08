"""Shared fixtures. Daemon fixtures are added in tests/daemon/conftest.py."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from gsched.clock import FakeClock
from gsched.platform.fake import FakePlatform

# 2026-11-08 12:00:00 UTC
T0_MS = 1794139200000


@pytest.fixture
def short_tmp():
    """sun_path <= 104: never use tmp_path for sockets."""
    d = tempfile.mkdtemp(prefix="gs-", dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clock():
    return FakeClock(start_ms=T0_MS)


@pytest.fixture
def plat(clock):
    return FakePlatform(memsize_gib=48, cpu=18, clock=clock)
