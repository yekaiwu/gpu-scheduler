"""Platform layer: everything that touches the OS goes through the Platform protocol (02)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol

from gsched.errors import PlatformError


@dataclass(frozen=True, slots=True)
class MachineInfo:
    memsize_bytes: int
    page_size: int
    physical_cpu: int
    perf_cores: int | None          # hw.perflevel0.physicalcpu
    eff_cores: int | None           # hw.perflevel1.physicalcpu
    chip: str                       # machdep.cpu.brand_string
    os_version: str                 # platform.mac_ver()[0]
    gpu_wired_limit_mb: int | None  # iogpu.wired_limit_mb; None if absent; 0 = OS default


@dataclass(frozen=True, slots=True)
class MemSample:
    t_ms: int
    free_pages: int                 # free_count - speculative_count (matches vm_stat "Pages free")
    purgeable_pages: int
    file_backed_pages: int          # external_page_count
    anonymous_pages: int            # internal_page_count (display only)
    wired_pages: int                # display only
    compressor_pages: int           # display only
    page_size: int
    level: int | None               # kern.memorystatus_level, None if unreadable
    swap_used_bytes: int | None     # None if unreadable -> fail closed
    swap_total_bytes: int | None

    @property
    def reclaimable_bytes(self) -> int:
        return (self.free_pages + self.purgeable_pages + self.file_backed_pages) * self.page_size


@dataclass(frozen=True, slots=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    uid: int
    start_sec: int
    start_usec: int
    comm: str                       # pbi_comm (16 bytes, truncated) for display only
    path: str | None                # proc_pidpath; None if unreadable
    footprint_bytes: int | None     # proc_pid_rusage V4 ri_phys_footprint; None if unreadable
    peak_footprint_bytes: int | None  # ri_lifetime_max_phys_footprint
    resident_bytes: int | None = None  # ri_resident_size. Used ONLY for job-tree accounting as
                                       # max(footprint, resident): mmap'd model weights (llama.cpp GGUF)
                                       # count in resident but not in phys_footprint. Never used alone.


@dataclass(frozen=True, slots=True)
class JobIdentity:
    pid: int
    start_sec: int
    start_usec: int
    path: str | None
    pgid: int


@dataclass(frozen=True, slots=True)
class GpuStats:
    device_util_pct: int | None
    in_use_bytes: int | None
    alloc_bytes: int | None


class AsyncProc(Protocol):
    """A helper subprocess the daemon never waits on (16 section 3, TCC probes and lsof lookups). Its stdout (stderr
    merged) is registered in the daemon's selector through `fileno()`; `poll()` never blocks."""
    def fileno(self) -> int: ...
    def poll(self) -> tuple[int | None, str] | None: ...    # None while running, else (returncode, output)
    def kill(self) -> None: ...                              # SIGKILL to this helper (a process we started)
    def close(self) -> None: ...


class Platform(Protocol):
    def machine(self) -> MachineInfo: ...
    def mem_sample(self, now_ms: int) -> MemSample: ...
    def processes(self) -> list[ProcInfo]: ...
    def proc(self, pid: int) -> ProcInfo | None: ...
    def proc_argv(self, pid: int) -> list[str] | None: ...
    def identity_matches(self, ident: JobIdentity) -> bool: ...
    def signal_pid(self, ident: JobIdentity, sig: int) -> bool: ...
    def signal_group(self, ident: JobIdentity, sig: int) -> bool: ...
    def pid_alive(self, pid: int) -> bool: ...
    def peer_credentials(self, sock) -> tuple[int, int]: ...
    def port_listening(self, port: int) -> bool: ...
    def port_owner_pids(self, port: int) -> list[int]: ...
    def port_owner_start(self, port: int) -> AsyncProc: ...
    def spawn_async(self, argv: list[str]) -> AsyncProc: ...
    def kill_async(self, proc: AsyncProc) -> None: ...      # SIGKILL a helper started by spawn_async/port_owner_start
    def gpu_stats(self) -> GpuStats: ...
    def caffeinate(self, pid: int) -> int | None: ...
    def spawn_shim(self, argv, *, env, cwd, stdout_path, stderr_path, pass_fds) -> int: ...
    def reap(self) -> list[tuple[int, int]]: ...
    def layout_report(self) -> dict: ...


def get_platform() -> Platform:
    """Darwin on darwin/arm64, else raises PlatformError (daemon exit 78)."""
    if sys.platform != "darwin":
        raise PlatformError(f"unsupported platform {sys.platform!r}: gsched requires macOS on Apple silicon")
    from gsched.platform.darwin import DarwinPlatform
    return DarwinPlatform()
