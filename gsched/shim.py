"""The per-job supervisor process: `python -I -m gsched.shim` (07 section 3).

Keep this import-light: stdlib only. Do NOT import gsched.daemon or gsched.store.

The shim runs in its own session (pgid == its pid). It forks the workload, which blocks on the release
gate until the daemon has committed the workload's identity. The shim then waits for the workload and
atomically writes exit.json, so exit status survives a daemon restart.
"""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time

_SIGS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2)


def _noop(signum, frame):
    pass


def _parse(argv: list[str]) -> dict:
    a: dict = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--":
            a["command"] = argv[i + 1:]
            break
        if argv[i] in ("--job", "--gate-fd", "--report-fd", "--exit-file", "--cwd"):
            a[argv[i][2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            raise SystemExit(f"gsched.shim: unexpected argument {argv[i]!r}")
    for k in ("job", "gate_fd", "report_fd", "exit_file", "command"):
        if k not in a or (k == "command" and not a[k]):
            raise SystemExit(f"gsched.shim: missing {k}")
    a["gate_fd"], a["report_fd"] = int(a["gate_fd"]), int(a["report_fd"])
    return a


def _peak_footprint(pid: int) -> int | None:
    """ri_lifetime_max_phys_footprint of the (zombie) workload. None on any failure."""
    try:
        import ctypes
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        libproc.proc_pid_rusage.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(296)
        if libproc.proc_pid_rusage(pid, 4, buf) != 0:
            return None
        v = int.from_bytes(buf.raw[240:248], "little")
        return v or None
    except Exception:
        return None


def _atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    a = _parse(argv)
    for s in _SIGS:
        signal.signal(s, _noop)  # the shim survives group signals; the child resets them before exec
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    signal.signal(signal.SIGCHLD, _noop)   # the wakeup fd makes select() return promptly when the workload exits
    pid = os.fork()
    if pid == 0:  # workload
        try:
            signal.set_wakeup_fd(-1)
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            for s in _SIGS:
                signal.signal(s, signal.SIG_DFL)
            # The Python runtime ignores SIGPIPE and SIGXFSZ; an ignored disposition survives exec, so without this
            # every job would run with them ignored (`producer | head` then fails with EPIPE instead of exiting).
            for s in (signal.SIGPIPE, signal.SIGXFSZ):
                signal.signal(s, signal.SIG_DFL)
            b = os.read(a["gate_fd"], 1)  # blocks until the daemon releases or closes
            os.close(a["gate_fd"])
            if b != b"1":
                os._exit(111)
            # chdir happens HERE, in the workload, never in the daemon: under launchd a path in a TCC-protected folder
            # blocks inside chdir()/open() waiting for a consent prompt. The daemon waits (without blocking) for the
            # "cwd_ok" line below and kills the job if it never comes (end_reason cwd_blocked, 16 / urgent addendum).
            if a.get("cwd"):
                try:
                    os.chdir(a["cwd"])
                except OSError as e:
                    os.write(2, f"gsched: cannot chdir to {a['cwd']}: {e}\n".encode())
                    os._exit(126)
            try:
                os.write(a["report_fd"], b"cwd_ok\n")
                os.close(a["report_fd"])
            except OSError:
                pass
            try:
                os.execvpe(a["command"][0], a["command"], os.environ)
            except OSError as e:
                os.write(2, f"gsched: exec failed: {e}\n".encode())
                os._exit(127)
        finally:
            os._exit(111)
    os.close(a["gate_fd"])
    try:
        os.write(a["report_fd"], f"{pid}\n".encode())
        os.close(a["report_fd"])
    except OSError:
        pass
    # os.waitid does not exist on macOS Python, so the shim cannot inspect the zombie before reaping.
    # It polls the workload's lifetime-max footprint while waiting, so the last reading is close to the true peak.
    peak = None
    status = 0
    while True:
        try:
            done, st = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            break
        if done == pid:
            status = st
            break
        cur = _peak_footprint(pid)
        if cur and (peak is None or cur > peak):
            peak = cur
        try:
            select.select([wake_r], [], [], 0.25)
            while os.read(wake_r, 4096):
                pass
        except (BlockingIOError, InterruptedError, OSError):
            pass
    rec: dict = {"v": 1, "job": a["job"], "pid": pid, "t_ms": int(time.time() * 1000), "peak_footprint_bytes": peak}
    att = os.environ.get("GSCHED_ATTEMPT")
    if att and att.isdigit():
        rec["attempt"] = int(att)
    if os.WIFEXITED(status):
        rec["exit_code"] = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status):
        rec["signal"] = os.WTERMSIG(status)
    _atomic_write_json(a["exit_file"], rec)
    return rec.get("exit_code", 128 + rec.get("signal", 0)) & 0xFF


if __name__ == "__main__":
    os._exit(main(sys.argv[1:]))
