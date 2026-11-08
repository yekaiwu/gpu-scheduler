"""ctypes structures for libproc / sysctl, with static offset checks (02 section 4)."""

from __future__ import annotations

import ctypes

from gsched.errors import PlatformError


class ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32), ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32), ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32), ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class XswUsage(ctypes.Structure):
    _fields_ = [("xsu_total", ctypes.c_uint64), ("xsu_avail", ctypes.c_uint64),
                ("xsu_used", ctypes.c_uint64), ("xsu_pagesize", ctypes.c_uint32),
                ("xsu_encrypted", ctypes.c_uint32)]


PROC_BSDINFO_SIZE = 136
RUSAGE_V4_SIZE = 296
RI_RESIDENT_SIZE_OFF = 64
RI_PHYS_FOOTPRINT_OFF = 72
RI_LIFETIME_MAX_FOOTPRINT_OFF = 240
SZOMB = 5

# vm_statistics64 offsets (little-endian u32)
VM_OFFSETS = {
    "free_count": 0, "active_count": 4, "inactive_count": 8, "wire_count": 12,
    "purgeable_count": 88, "speculative_count": 92, "compressor_page_count": 128,
    "throttled_count": 132, "external_page_count": 136, "internal_page_count": 140,
}
VM_MIN_COUNT = 38  # the original 152-byte struct


def static_check() -> None:
    """Size and offset assertions that do not need the kernel. Raises PlatformError."""
    if ctypes.sizeof(ProcBsdInfo) != PROC_BSDINFO_SIZE:
        raise PlatformError(f"layout check failed: sizeof(proc_bsdinfo) = {ctypes.sizeof(ProcBsdInfo)}, want 136")
    if ProcBsdInfo.pbi_pgid.offset != 100:
        raise PlatformError("layout check failed: pbi_pgid offset")
    if ProcBsdInfo.pbi_start_tvsec.offset != 120 or ProcBsdInfo.pbi_start_tvusec.offset != 128:
        raise PlatformError("layout check failed: pbi_start_tv offsets")
    if ProcBsdInfo.pbi_comm.offset != 48 or ProcBsdInfo.pbi_uid.offset != 20:
        raise PlatformError("layout check failed: pbi_comm/pbi_uid offsets")
    if ctypes.sizeof(XswUsage) != 32:
        raise PlatformError(f"layout check failed: sizeof(xsw_usage) = {ctypes.sizeof(XswUsage)}, want 32")
