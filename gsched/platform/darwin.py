"""Darwin implementation of the Platform protocol, built on ctypes (02).

This module and `fake.py` are the ONLY places in `gsched/` allowed to send signals
(`os.kill` / `os.killpg`). Both go through `signal_pid` / `signal_group`, which
re-verify the process identity immediately before sending.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform as _pyplatform
import re
import select
import socket
import struct
import subprocess
import sys
import time

from gsched.errors import PlatformError
from gsched.platform import GpuStats, JobIdentity, MachineInfo, MemSample, ProcInfo
from gsched.platform import layouts as L

PROC_PIDTBSDINFO = 3
RUSAGE_INFO_V4 = 4
PROC_PIDPATHINFO_MAXSIZE = 4096
HOST_VM_INFO64 = 4
SOL_LOCAL, LOCAL_PEERPID = 0, 2
CTL_KERN, KERN_PROCARGS2 = 1, 49
LSOF = "/usr/sbin/lsof"
IOREG = "/usr/sbin/ioreg"
CAFFEINATE = "/usr/bin/caffeinate"


def _load_libs():
    libc = ctypes.CDLL(None, use_errno=True)
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                  ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctlbyname.restype = ctypes.c_int
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    libc.mach_host_self.restype = ctypes.c_uint32
    libc.mach_host_self.argtypes = []
    libc.host_statistics64.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_uint32)]
    libc.host_statistics64.restype = ctypes.c_int
    libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    libc.getpeereid.restype = ctypes.c_int
    libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libproc.proc_listallpids.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int
    libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    libproc.proc_pid_rusage.restype = ctypes.c_int
    return libc, libproc


class DarwinPlatform:
    def __init__(self, *, skip_os_check: bool = False):
        # 1-4: platform checks, in order
        if sys.platform != "darwin":
            raise PlatformError("gsched requires macOS (sys.platform != 'darwin')")
        L.static_check()
        self.libc, self.libproc = _load_libs()
        arm = self.sysctl_int("hw.optional.arm64")
        if _pyplatform.machine() != "arm64":
            if arm == 1:
                raise PlatformError("running under Rosetta; use a native arm64 Python")
            raise PlatformError("gsched requires Apple silicon (arm64)")
        if arm != 1:
            raise PlatformError("hw.optional.arm64 != 1: gsched requires Apple silicon")
        try:
            major = int((_pyplatform.mac_ver()[0] or "0").split(".")[0])
        except ValueError:
            major = 0
        if major < 14 and not skip_os_check and os.environ.get("GSCHED_ALLOW_OLD_MACOS") != "1":
            raise PlatformError(f"macOS {_pyplatform.mac_ver()[0]} is older than 14; "
                                "set GSCHED_ALLOW_OLD_MACOS=1 to bypass (testers only)")
        self._host = self.libc.mach_host_self()  # cached: every call allocates a send right
        self._popens: dict[int, subprocess.Popen] = {}
        self._gpu_cache: tuple[float, GpuStats] | None = None
        self._caffeinate_missing_logged = False
        self._uid = os.getuid()
        self._pagesize = self.sysctl_int("hw.pagesize") or os.sysconf("SC_PAGE_SIZE")
        self._layout: dict = {}
        self._self_check()
        self._machine = self._build_machine()

    # ---- sysctl ---------------------------------------------------------
    def sysctl_raw(self, name: str, size: int) -> bytes | None:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t(size)
        if self.libc.sysctlbyname(name.encode(), buf, ctypes.byref(n), None, 0) != 0:
            return None
        return buf.raw[: n.value]

    def sysctl_int(self, name: str) -> int | None:
        raw = self.sysctl_raw(name, 8)
        if raw is None or len(raw) not in (4, 8):
            return None
        return int.from_bytes(raw, "little", signed=False)

    def sysctl_str(self, name: str) -> str | None:
        n = ctypes.c_size_t(0)
        if self.libc.sysctlbyname(name.encode(), None, ctypes.byref(n), None, 0) != 0:
            return None
        buf = ctypes.create_string_buffer(n.value + 1)
        n2 = ctypes.c_size_t(n.value)
        if self.libc.sysctlbyname(name.encode(), buf, ctypes.byref(n2), None, 0) != 0:
            return None
        return buf.value.decode("utf-8", "replace")

    # ---- machine / memory -----------------------------------------------
    def _build_machine(self) -> MachineInfo:
        memsize = self.sysctl_int("hw.memsize")
        if not memsize:
            raise PlatformError("cannot read hw.memsize")
        return MachineInfo(
            memsize_bytes=memsize, page_size=self._pagesize,
            physical_cpu=self.sysctl_int("hw.physicalcpu") or os.cpu_count() or 1,
            perf_cores=self.sysctl_int("hw.perflevel0.physicalcpu"),
            eff_cores=self.sysctl_int("hw.perflevel1.physicalcpu"),
            chip=self.sysctl_str("machdep.cpu.brand_string") or "unknown",
            os_version=_pyplatform.mac_ver()[0],
            gpu_wired_limit_mb=self.sysctl_int("iogpu.wired_limit_mb"))

    def machine(self) -> MachineInfo:
        return self._machine

    def _vm_stats(self) -> dict[str, int]:
        buf = (ctypes.c_uint32 * 256)()
        count = ctypes.c_uint32(256)
        kr = self.libc.host_statistics64(self._host, HOST_VM_INFO64, buf, ctypes.byref(count))
        if kr != 0 or count.value < L.VM_MIN_COUNT:
            raise PlatformError(f"host_statistics64 failed (kr={kr}, count={count.value})")
        raw = bytes(buf)
        return {k: struct.unpack_from("<I", raw, off)[0] for k, off in L.VM_OFFSETS.items()}

    def mem_sample(self, now_ms: int) -> MemSample:
        try:
            v = self._vm_stats()
        except PlatformError:
            # unreadable: report zero reclaimable (fail closed: nothing fits)
            v = {k: 0 for k in L.VM_OFFSETS}
        level = self.sysctl_int("kern.memorystatus_level")
        swap_used = swap_total = None
        raw = self.sysctl_raw("vm.swapusage", ctypes.sizeof(L.XswUsage))
        if raw is not None and len(raw) == ctypes.sizeof(L.XswUsage):
            x = L.XswUsage.from_buffer_copy(raw)
            swap_used, swap_total = int(x.xsu_used), int(x.xsu_total)
        return MemSample(
            t_ms=now_ms, free_pages=max(0, v["free_count"] - v["speculative_count"]),
            purgeable_pages=v["purgeable_count"], file_backed_pages=v["external_page_count"],
            anonymous_pages=v["internal_page_count"], wired_pages=v["wire_count"],
            compressor_pages=v["compressor_page_count"], page_size=self._pagesize,
            level=level, swap_used_bytes=swap_used, swap_total_bytes=swap_total)

    # ---- processes --------------------------------------------------------
    def _bsdinfo(self, pid: int):
        info = L.ProcBsdInfo()
        n = self.libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
        if n != L.PROC_BSDINFO_SIZE:
            return None
        return info

    def _path(self, pid: int, sec: int, usec: int) -> str | None:
        # Never cached: exec() replaces the executable but keeps pid and start time, so a cache keyed by
        # (pid, start) returns the pre-exec path forever (and a later cache flush made every running job
        # look like a different process). A fresh proc_pidpath per process costs well under 10 ms per scan.
        buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        n = self.libproc.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
        return os.fsdecode(buf.value) if n > 0 else None

    def _rusage3(self, pid: int) -> tuple[int | None, int | None, int | None]:
        buf = ctypes.create_string_buffer(L.RUSAGE_V4_SIZE)
        if self.libproc.proc_pid_rusage(pid, RUSAGE_INFO_V4, buf) != 0:
            return None, None, None
        raw = buf.raw
        return (struct.unpack_from("<Q", raw, L.RI_PHYS_FOOTPRINT_OFF)[0],
                struct.unpack_from("<Q", raw, L.RI_LIFETIME_MAX_FOOTPRINT_OFF)[0],
                struct.unpack_from("<Q", raw, L.RI_RESIDENT_SIZE_OFF)[0])

    def _rusage(self, pid: int) -> tuple[int | None, int | None]:
        fp, peak, _ = self._rusage3(pid)
        return fp, peak

    def _procinfo(self, pid: int, info, with_rusage=True) -> ProcInfo:
        fp, peak, res = self._rusage3(pid) if with_rusage else (None, None, None)
        return ProcInfo(
            pid=pid, ppid=info.pbi_ppid, pgid=info.pbi_pgid, uid=info.pbi_uid,
            start_sec=info.pbi_start_tvsec, start_usec=info.pbi_start_tvusec,
            comm=info.pbi_comm.decode("utf-8", "replace"),
            path=self._path(pid, info.pbi_start_tvsec, info.pbi_start_tvusec),
            footprint_bytes=fp, peak_footprint_bytes=peak, resident_bytes=res)

    def proc(self, pid: int) -> ProcInfo | None:
        if pid <= 0:
            return None
        info = self._bsdinfo(pid)
        if info is None or info.pbi_pid != pid or info.pbi_status == L.SZOMB:
            return None
        return self._procinfo(pid, info)

    def processes(self) -> list[ProcInfo]:
        n = self.libproc.proc_listallpids(None, 0)
        if n <= 0:
            # never "no processes": that would read as no foreign servers and every job's footprint unreadable
            raise PlatformError("proc_listallpids failed")
        buf = (ctypes.c_int * (n + 64))()
        got = self.libproc.proc_listallpids(buf, ctypes.sizeof(buf))
        if got <= 0:
            raise PlatformError("proc_listallpids failed")
        out = []
        for pid in list(buf)[: max(got, 0)]:
            if pid == 0:
                continue
            info = self._bsdinfo(pid)
            if info is None or info.pbi_status == L.SZOMB:
                continue
            out.append(self._procinfo(pid, info))
        return out

    def proc_argv(self, pid: int) -> list[str] | None:
        size = ctypes.c_size_t(262144)
        buf = ctypes.create_string_buffer(size.value)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        if self.libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0 or size.value < 8:
            return None
        raw = buf.raw[: size.value]
        argc = struct.unpack_from("<i", raw, 0)[0]
        rest = raw[4:]
        i = rest.find(b"\0")
        if i < 0:
            return None
        while i < len(rest) and rest[i] == 0:
            i += 1
        parts = rest[i:].split(b"\0")
        return [p.decode("utf-8", "replace") for p in parts[: max(argc, 0)]]

    def identity_matches(self, ident: JobIdentity) -> bool:
        p = self.proc(ident.pid)
        if p is None:
            return False
        if p.start_sec != ident.start_sec or p.start_usec != ident.start_usec:
            return False
        if ident.path is not None and p.path is not None and p.path != ident.path:
            return False
        return True

    def pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)  # signal 0: existence probe only, sends nothing
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        info = self._bsdinfo(pid)  # zombies (or a pid that just vanished) are not alive
        return info is not None and info.pbi_status != L.SZOMB

    # ---- signals: the only senders in gsched/ -------------------------------
    def signal_pid(self, ident: JobIdentity, sig: int) -> bool:
        if not self.identity_matches(ident):
            return False
        try:
            os.kill(ident.pid, sig)
            return True
        except ProcessLookupError:
            return False

    def signal_group(self, ident: JobIdentity, sig: int) -> bool:
        if not self.identity_matches(ident):
            return False
        p = self.proc(ident.pid)
        if p is None or p.pgid != ident.pgid or ident.pgid <= 1:
            return False
        try:
            os.killpg(ident.pgid, sig)
            return True
        except ProcessLookupError:
            return False

    # ---- sockets ----------------------------------------------------------------
    def peer_credentials(self, sock) -> tuple[int, int]:
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        if self.libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        pid = struct.unpack("i", sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
        return uid.value, pid

    def port_listening(self, port: int) -> bool:
        for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                s = socket.socket(fam, socket.SOCK_STREAM)
            except OSError:
                continue
            s.setblocking(False)
            try:
                rc = s.connect_ex((addr, port))
                if rc == 0:
                    return True
                if rc in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                    _, w, _ = select.select([], [s], [], 0.05)
                    if w and s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                        return True
            except OSError:
                pass
            finally:
                s.close()
        return False

    def kill_async(self, proc) -> None:
        proc.kill()

    def port_owner_start(self, port: int) -> "DarwinAsyncProc":
        return self.spawn_async([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])

    def spawn_async(self, argv: list[str]) -> "DarwinAsyncProc":
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True, close_fds=True)
        self._popens[p.pid] = p
        return DarwinAsyncProc(p)

    def port_owner_pids(self, port: int) -> list[int]:
        try:
            r = subprocess.run([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                               capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(x) for x in r.stdout.split() if x.isdigit()]

    # ---- gpu / caffeinate ---------------------------------------------------------
    def gpu_stats(self) -> GpuStats:
        now = time.monotonic()
        if self._gpu_cache and now - self._gpu_cache[0] < 5.0:
            return self._gpu_cache[1]
        stats = GpuStats(None, None, None)
        try:
            r = subprocess.run([IOREG, "-r", "-d", "1", "-w", "0", "-c", "AGXAccelerator"],
                               capture_output=True, text=True, timeout=1)
            for line in r.stdout.splitlines():
                if '"PerformanceStatistics"' in line:
                    def grab(key):
                        m = re.search(r'"%s"=(\d+)' % re.escape(key), line)
                        return int(m.group(1)) if m else None
                    stats = GpuStats(grab("Device Utilization %"), grab("In use system memory"),
                                     grab("Alloc system memory"))
                    break
        except (OSError, subprocess.SubprocessError):
            pass
        self._gpu_cache = (now, stats)
        return stats

    def caffeinate(self, pid: int) -> int | None:
        if not os.path.exists(CAFFEINATE):
            return None
        try:
            p = subprocess.Popen([CAFFEINATE, "-i", "-w", str(pid)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return None
        self._popens[p.pid] = p
        return p.pid

    # ---- spawning / reaping -----------------------------------------------------------
    def spawn_shim(self, argv, *, env, cwd, stdout_path, stderr_path, pass_fds) -> int:
        fo = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fe = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError:
            os.close(fo)
            raise
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                 pass_fds=tuple(pass_fds), start_new_session=True, close_fds=True)
        finally:
            os.close(fo)
            os.close(fe)
        self._popens[p.pid] = p  # kept only to avoid ResourceWarning; reaping is by waitpid(-1)
        return p.pid

    def reap(self) -> list[tuple[int, int]]:
        out = []
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            out.append((pid, status))
            p = self._popens.pop(pid, None)
            if p is not None:
                p.returncode = os.waitstatus_to_exitcode(status)  # reported here; avoid a second wait
        return out

    # ---- self check -----------------------------------------------------------------------
    def _self_check(self) -> None:
        me = os.getpid()
        info = self._bsdinfo(me)
        if info is None:
            raise PlatformError("layout check failed: proc_pidinfo(self) did not return 136 bytes")
        if info.pbi_pid != me:
            raise PlatformError("layout check failed: pbi_pid mismatch")
        if info.pbi_uid != os.getuid():
            raise PlatformError("layout check failed: pbi_uid mismatch")
        if info.pbi_pgid != os.getpgid(0):
            raise PlatformError("layout check failed: pbi_pgid mismatch")
        if info.pbi_ppid != os.getppid():
            raise PlatformError("layout check failed: pbi_ppid mismatch")
        if not (0 < info.pbi_start_tvsec <= int(time.time()) + 1) or info.pbi_start_tvusec >= 1_000_000:
            raise PlatformError("layout check failed: pbi_start_tv out of range")
        fp, _ = self._rusage(me)
        if fp is None or not (0 < fp < 4 * 2**30):
            raise PlatformError("layout check failed: ri_phys_footprint of self implausible")
        v = self._vm_stats()
        memsize = self.sysctl_int("hw.memsize") or 0
        total = v["free_count"] + v["active_count"] + v["inactive_count"] + v["wire_count"] + v["speculative_count"]
        if memsize and total > memsize / self._pagesize * 1.1:
            raise PlatformError("layout check failed: vm_statistics64 page counts exceed memory size")
        a, b = socket.socketpair()
        try:
            uid, pid = self.peer_credentials(a)
            if pid != me or uid != os.getuid():
                raise PlatformError("layout check failed: peer credentials of a socketpair")
        except OSError as e:
            raise PlatformError(f"layout check failed: peer credentials: {e}")
        finally:
            a.close()
            b.close()
        raw = self.sysctl_raw("vm.swapusage", ctypes.sizeof(L.XswUsage))
        if raw is None or len(raw) != 32:
            raise PlatformError("layout check failed: vm.swapusage")
        self._layout = {"proc_bsdinfo_size": ctypes.sizeof(L.ProcBsdInfo), "xsw_usage_size": 32,
                        "vm_stat_count_ok": True, "self_footprint_bytes": fp}

    def layout_report(self) -> dict:
        return dict(self._layout, ok=True)


class DarwinAsyncProc:
    """Non-blocking helper process (see platform.AsyncProc). Output is capped at 64 KiB."""

    def __init__(self, p: subprocess.Popen):
        self._p = p
        self._buf = bytearray()
        self._eof = False
        os.set_blocking(p.stdout.fileno(), False)

    def fileno(self) -> int:
        return self._p.stdout.fileno()

    def poll(self):
        while not self._eof:
            try:
                chunk = os.read(self._p.stdout.fileno(), 65536)
            except (BlockingIOError, InterruptedError):
                return None
            except OSError:
                chunk = b""
            if not chunk:
                self._eof = True
            elif len(self._buf) < 65536:
                self._buf += chunk
        rc = self._p.poll()
        if rc is None:
            return None
        return rc, bytes(self._buf).decode("utf-8", "replace")

    def kill(self) -> None:
        try:
            self._p.kill()           # SIGKILL to the helper we started; never to anything else
        except (OSError, ProcessLookupError):
            pass

    def close(self) -> None:
        try:
            self._p.stdout.close()
        except OSError:
            pass


# ---- helpers for the `gsched exec` wrapper (client side, no Platform instance) --------------------------------
# The wrapper is a client process, not the daemon, but ground rule 1 keeps every signal send in this file.

def signal_own_child(child: subprocess.Popen, signo: int) -> bool:
    """Forward `signo` to the wrapper's own direct child. Popen.send_signal refuses once the child has been reaped, so a
    recycled pid is never hit. Used for SIGTERM/INT/HUP/USR1/USR2/QUIT forwarding in `gsched exec`."""
    try:
        child.send_signal(signo)
        return True
    except (OSError, ProcessLookupError):
        return False


def exit_by_signal(signo: int) -> None:
    """Die of `signo` ourselves (default disposition restored first) so our parent sees the child's exact fate."""
    import signal as _sig
    try:
        _sig.signal(signo, _sig.SIG_DFL)
    except (OSError, ValueError):
        pass                                    # SIGKILL / SIGSTOP cannot be reset; they need no reset
    _sig.raise_signal(signo)
