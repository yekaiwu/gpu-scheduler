# 02: Platform layer

Everything that touches the OS goes through one protocol. The Darwin implementation uses ctypes. The fake is a deterministic, in-memory model used by almost every test.

## 1. Protocol

`gsched/platform/__init__.py`:

```python
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

class Platform(Protocol):
    def machine(self) -> MachineInfo: ...
    def mem_sample(self, now_ms: int) -> MemSample: ...
    def processes(self) -> list[ProcInfo]: ...                 # all visible pids, one pass
    def proc(self, pid: int) -> ProcInfo | None: ...
    def identity_matches(self, ident: JobIdentity) -> bool: ...
    def signal_pid(self, ident: JobIdentity, sig: int) -> bool: ...    # re-verifies, then os.kill
    def signal_group(self, ident: JobIdentity, sig: int) -> bool: ...  # re-verifies, then os.killpg(ident.pgid)
    def pid_alive(self, pid: int) -> bool: ...                 # kill(pid, 0); EPERM counts as alive
    def peer_credentials(self, sock) -> tuple[int, int]: ...   # (uid, pid)
    def port_listening(self, port: int) -> bool: ...
    def port_owner_pids(self, port: int) -> list[int]: ...     # lsof -t; may be slow; rare use
    def gpu_stats(self) -> GpuStats: ...
    def caffeinate(self, pid: int) -> int | None: ...          # returns caffeinate pid
    def spawn_shim(self, argv, *, env, cwd, stdout_path, stderr_path, pass_fds) -> int: ...
    def reap(self) -> list[tuple[int, int]]: ...               # [(pid, raw_status)] via waitpid(-1, WNOHANG)

def get_platform() -> Platform  # Darwin on darwin/arm64, else raises PlatformError (exit 78)
```

`signal_pid` and `signal_group` are the **only** places in `gsched/` that call `os.kill` or `os.killpg`. Both call `identity_matches` immediately before sending and return False without sending on a mismatch. A test greps the source tree to enforce this (`test_only_platform_sends_signals`).

## 2. Startup checks (`darwin.py: DarwinPlatform.__init__`)

In order. Any failure raises `PlatformError`, and the daemon exits 78 before it listens:

1. `sys.platform == "darwin"`.
2. `platform.machine() == "arm64"`. If it isn't, check `sysctl hw.optional.arm64`: if that is 1, the message is "running under Rosetta; use a native arm64 Python".
3. `sysctlbyname("hw.optional.arm64") == 1`.
4. `platform.mac_ver()[0]` major ≥ 14. Lower versions are refused with a message, though there's no technical block; set `GSCHED_ALLOW_OLD_MACOS=1` to bypass, for testers.
5. Layout self-check (§4).

`gsched doctor` and the CLI do **not** need these checks (they only use the socket), except `doctor`'s local section, which runs them and reports.

## 3. ctypes bindings

Load libraries once:

```python
libc = ctypes.CDLL(None, use_errno=True)                         # libSystem
libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)  # resolves via dyld shared cache
```

Set `argtypes` and `restype` for **every** function. Defaults (int) truncate pointers and 64-bit values.

### 3.1 sysctl

```python
libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
libc.sysctlbyname.restype = ctypes.c_int

def sysctl_int(name: str, ctype=ctypes.c_int64) -> int | None
def sysctl_str(name: str) -> str | None   # call with oldp=None to get length first
def sysctl_raw(name: str, size: int) -> bytes | None
```

| Name | C type | Use |
|---|---|---|
| `hw.memsize` | uint64 | `MachineInfo.memsize_bytes` |
| `hw.pagesize` | int64 (read as c_int64; kernel returns a 64-bit int here) | page size. Cross-check with `host_page_size`; if they disagree, use `hw.pagesize` and log |
| `hw.physicalcpu`, `hw.perflevel0.physicalcpu`, `hw.perflevel1.physicalcpu` | int32 | cores. perflevel keys may be absent → None |
| `hw.optional.arm64` | int32 | platform check |
| `machdep.cpu.brand_string` | string | chip name |
| `kern.memorystatus_level` | int32 | 0–100 |
| `vm.swapusage` | `struct xsw_usage` (32 bytes) | swap |
| `iogpu.wired_limit_mb` | int32 | absent on older macOS → None |

Read 32-bit sysctls into a `c_int32` buffer with `oldlen=4`, and 64-bit ones into `c_int64` with `oldlen=8`. If the returned `oldlen` differs from what you expected, accept 4 or 8 and decode accordingly. Write one helper for this.

`xsw_usage`:

```python
class XswUsage(ctypes.Structure):
    _fields_ = [("xsu_total", ctypes.c_uint64), ("xsu_avail", ctypes.c_uint64),
                ("xsu_used", ctypes.c_uint64), ("xsu_pagesize", ctypes.c_uint32),
                ("xsu_encrypted", ctypes.c_uint32)]   # boolean_t is int
# assert ctypes.sizeof(XswUsage) == 32
```

### 3.2 Host VM statistics

```python
libc.mach_host_self.restype = ctypes.c_uint32
libc.mach_host_self.argtypes = []
libc.host_statistics64.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
libc.host_statistics64.restype = ctypes.c_int
HOST_VM_INFO64 = 4
```

- Call `mach_host_self()` **once** at init and cache the port. Every call allocates a send right, which leaks if repeated.
- Buffer: `(ctypes.c_uint32 * 256)()`, `count = c_uint32(256)`. After the call, require `kr == 0` and `count.value >= 38` (the original 152-byte struct). **Do not assert a fixed size.** The verified machine returned 40, while its SDK says 104.
- Decode by offset from the raw bytes (little-endian `struct.unpack_from("<I", raw, off)`):

| Field | Offset | Type |
|---|---|---|
| `free_count` | 0 | u32 |
| `active_count` | 4 | u32 |
| `inactive_count` | 8 | u32 |
| `wire_count` | 12 | u32 |
| `purgeable_count` | 88 | u32 |
| `speculative_count` | 92 | u32 |
| `compressor_page_count` | 128 | u32 |
| `throttled_count` | 132 | u32 |
| `external_page_count` | 136 | u32 |
| `internal_page_count` | 140 | u32 |

- `free_pages = free_count - speculative_count` (this matches `vm_stat`'s "Pages free" line; vm_stat prints speculative separately).
- Reclaimable = `(free_pages + purgeable + external) * page_size`. This is the same sum people already measure with `vm_stat` (free + purgeable + file-backed). Purgeable pages can also be counted within the other classes, so the sum may over-count by up to the purgeable figure. Keep the formula anyway for comparability, and record all three parts separately.

### 3.3 libproc

```python
libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]; restype = ctypes.c_int   # returns count
libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]; restype = ctypes.c_int
libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]; restype = ctypes.c_int
libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]; restype = ctypes.c_int
PROC_PIDTBSDINFO = 3
RUSAGE_INFO_V4 = 4
PROC_PIDPATHINFO_MAXSIZE = 4096
```

**Listing pids:** call `proc_listallpids(None, 0)` to get an estimate `n`, allocate `(c_int * (n + 64))`, and call again with `bufsize = sizeof(buf)`. The return value is the number of pids. Drop zeros.

**`proc_bsdinfo`** (136 bytes). Define the full structure and check it (§4):

```python
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
```

`proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, byref(info), sizeof(info))` must return exactly 136. Anything else means that process is unreadable (it exited, or belongs to another user / is protected). Return `None` for that pid. That's not an error.

**Zombies:** `pbi_status == 5` (`SZOMB`). Treat a zombie as **not alive** for identity purposes, but don't reuse its pid until reaped.

**Path:** `proc_pidpath(pid, buf, 4096)`. Return > 0 means success. Decode with `os.fsdecode`.

**Footprint:** a `rusage_info_v4` buffer of 296 bytes. `proc_pid_rusage(pid, RUSAGE_INFO_V4, buf)` returns 0 on success. Read `ri_phys_footprint` at offset 72 and `ri_lifetime_max_phys_footprint` at offset 240 (u64). You can't read other users' processes. That's fine (None).

Never fall back to RSS. There is no RSS field in `ProcInfo`.

**Cost:** the full scan (`processes()`) runs once per tick. About 800 pids × (bsdinfo + rusage) takes about 5–20 ms. If `proc_listallpids` fails, `processes()` raises `PlatformError`; it never returns an empty list for "unknown" (ground rule 9). The tick then keeps the last scan, skips bookkeeping, and admits nothing until a scan succeeds (event `scan_failed`). Read `proc_pidpath` fresh on every call. **Do not cache it by `(pid, start_sec, start_usec)`:** `exec` replaces the executable but keeps the pid and the start time, so such a cache returns the pre-exec path forever (the recorded identity becomes the shim's Python), and a cache flush then makes every running job look like a different process. Measured cost on the reference machine: about 600 pids, under 10 ms for the whole scan including the path reads.

### 3.4 Peer credentials

```python
libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
libc.getpeereid.restype = ctypes.c_int
SOL_LOCAL, LOCAL_PEERPID = 0, 2
pid = struct.unpack("i", sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
```

Both are verified on the reference machine. A uid mismatch closes the connection after sending `{"error":{"code":"forbidden"}}`.

### 3.5 Signals

```python
def signal_pid(self, ident, sig):
    if not self.identity_matches(ident): return False
    try: os.kill(ident.pid, sig); return True
    except ProcessLookupError: return False
def signal_group(self, ident, sig):
    if not self.identity_matches(ident): return False
    try: os.killpg(ident.pgid, sig); return True
    except ProcessLookupError: return False
```

`identity_matches`: `proc(pid)` is not None, isn't a zombie, `start_sec`/`start_usec` match, and `path` matches when both are known. For `signal_group`, also require `proc(pid).pgid == ident.pgid`.

There is a small time-of-check/time-of-use window between the check and `kill`. It's acceptable: pid reuse inside microseconds on a single-user Mac is negligible, and the alternative (pidfd) doesn't exist on macOS.

### 3.6 Port probe

```python
def port_listening(self, port):
    for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        s = socket.socket(fam, socket.SOCK_STREAM); s.setblocking(False)
        try:
            rc = s.connect_ex((addr, port))
            if rc == 0: return True
            if rc in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                _, w, _ = select.select([], [s], [], 0.05)
                if w and s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0: return True
        finally: s.close()
    return False
```

At most 50 ms per address. The scan module calls it every `port_probe_s` for each configured port, so the worst case is `2 × 50 ms × ports`. If there are more than 4 ports, spread them across ticks (round-robin) to stay within the tick budget.

`port_owner_pids(port)`: `subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=2)`. Called only when a probe changes from not-listening to listening and the port isn't one of our jobs' `listen_port`. It's used only to fill `offender.pid`/`path`. Failure is fine.

### 3.7 GPU stats

`subprocess.run(["/usr/sbin/ioreg", "-r", "-d", "1", "-w", "0", "-c", "AGXAccelerator"], capture_output=True, timeout=1)`, at most every 5 s. Parse the first `"PerformanceStatistics" = {...}` line with a regex for `"Device Utilization %"=(\d+)`, `"In use system memory"=(\d+)`, and `"Alloc system memory"=(\d+)`. Any failure returns all None. This is informational only, unless `gpu_busy_foreign_pct` is set.

### 3.8 caffeinate

`subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(shim_pid)], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)`. It exits on its own when the shim exits. The daemon reaps it in `reap()` and ignores its status. If `/usr/bin/caffeinate` is missing, log once and continue.

### 3.9 Spawning

`spawn_shim` uses `subprocess.Popen(argv, cwd=cwd, env=env, stdin=DEVNULL, stdout=fd_out, stderr=fd_err, pass_fds=pass_fds, start_new_session=True, close_fds=True)`. Open the log files with `os.open(path, O_WRONLY|O_CREAT|O_APPEND, 0o600)` and close them in the daemon after Popen returns. Return `proc.pid`. **Don't keep the `Popen` object for waiting.** The daemon reaps with `os.waitpid(-1, WNOHANG)` in `reap()`, so a `Popen.__del__` can't race with it. Keep the Popen objects in a dict only to prevent ResourceWarnings, and drop them after reaping. (Simpler alternative: use `os.posix_spawn` with `setsid` via `POSIX_SPAWN_SETSID`, which Python exposes as `setsid=True` in 3.12. Either is fine. Pick one and document it in the code.)

## 4. Layout self-check (`layouts.py`)

At startup:

1. `ctypes.sizeof(ProcBsdInfo) == 136`, and `ProcBsdInfo.pbi_pgid.offset == 100`, `pbi_start_tvsec.offset == 120`.
2. `proc_pidinfo(os.getpid(), PROC_PIDTBSDINFO, ...)` returns 136, `pbi_pid == os.getpid()`, `pbi_uid == os.getuid()`, `pbi_pgid == os.getpgid(0)`, `pbi_ppid == os.getppid()`, `pbi_start_tvsec` is within 1 day of `time.time() - (time.monotonic() - <process start estimate>)`. A simpler check: `0 < pbi_start_tvsec <= time.time()` and `pbi_start_tvusec < 1_000_000`.
3. `proc_pid_rusage(os.getpid(), 4, buf) == 0` and `0 < footprint < 4 GiB`.
4. `host_statistics64` returns kr 0 with count ≥ 38, and `free + active + inactive + wire + speculative <= memsize / page_size * 1.1`.
5. `getsockopt(LOCAL_PEERPID)` on a `socketpair` returns `os.getpid()`. `getpeereid` returns `os.getuid()`.

Any failure → `PlatformError("layout check failed: <which>")` → exit 78. Signaling with an unverified layout is never allowed.

`tools/check_layouts.c` prints the same numbers from the SDK headers (the program in the review was run on 2026-11-08). The program is in the repo for contributors on future macOS versions: `cc tools/check_layouts.c -o /tmp/cl && /tmp/cl`.

## 5. The fake (`platform/fake.py`)

The fake models a machine and lets a test script processes:

```python
class FakePlatform(Platform):
    def __init__(self, memsize_gib=48, page_size=16384, cpu=18): ...
    # memory knobs
    def set_mem(self, *, reclaimable_gib=None, level=None, swap_used_gib=None, swap_unreadable=False): ...
    # processes
    def add_foreign(self, name, footprint_gib, *, path=None, port=None) -> int: ...  # returns pid
    def remove(self, pid): ...
    def set_footprint(self, pid, gib): ...
    def set_footprint_unreadable(self, pid, flag=True): ...
    def recycle_pid(self, pid): ...      # same pid, new start time -> identity mismatch
    # spawned jobs: spawn_shim creates a FakeShim + FakeWorkload pair
    def workload(self, job_id) -> FakeWorkload: ...
    # signal log
    signals: list[tuple[int, int, str]]   # (pid_or_-pgid, signo, job_id_or_"")
```

`FakeWorkload` behaviors, chosen by the test:

```python
w.on_signal(SIGUSR1, action="checkpoint_and_exit")   # write READY in the checkpoint dir, exit 0
w.on_signal(SIGUSR1, action="ignore")
w.on_signal(SIGTERM, action="exit", code=143)
w.exit(code=0)                       # finish now
w.set_footprint(gib)
w.spawn_child("llama-server", gib)   # descendant in same pgid
```

The fake `spawn_shim` does **not** start real processes. It simulates the shim protocol: it reports the workload pid on the report pipe (a real `os.pipe()` so the daemon's selector code is exercised), waits for the gate byte (the daemon writes to the real gate pipe; the fake reads it on the next `step`), and writes `exit.json` into the real job dir when the workload exits. Tests therefore exercise the real supervision code paths, including the files on disk.

The fake must also implement `reap()` (return fake shim exits), `pid_alive`, `identity_matches` (respecting `recycle_pid`), and `port_listening` (from `add_foreign(port=...)` and from fake workloads with a port).

Live tests (`@pytest.mark.live`) use `DarwinPlatform` with real short-lived processes (`/bin/sleep`, `python -c`) and the real shim. They are the contract tests for the ctypes code and the shim.
