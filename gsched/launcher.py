"""Helper for projects that must own their child process (spawn=external). Stdlib only; vendorable as one file (07 section 8).

    with gsched.launcher.admitted(spec) as lease:      # submit + wait --until start
        child = lease.spawn(argv, cwd=..., env=...)     # gate, attach, release the gate; kills the child on attach failure
        code = child.wait()
        lease.finish(code)

Wait for your own preconditions BEFORE calling admitted(), so you do not hold capacity while waiting.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys

from gsched import client

_BOOTSTRAP = (
    "import os,sys\n"
    "fd=int(sys.argv[1]); b=os.read(fd,1); os.close(fd)\n"
    "if b!=b'1': os._exit(111)\n"
    "try:\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except OSError as e:\n"
    "    os.write(2, ('gsched: exec failed: %s: %s\\n' % (sys.argv[2], e)).encode())\n"
    "    os._exit(126 if isinstance(e, PermissionError) else 127)\n"
)


class LauncherError(Exception):
    pass


class Lease:
    def __init__(self, job: str, token: str, home=None, socket_override=None):
        self.job = job
        self.token = token
        self._kw = {"home": home, "socket_override": socket_override}
        self.finished = False

    def _call(self, method: str, **params) -> dict:
        env = client.call(method, params, door="launcher", **self._kw)
        return client.result_or_raise(env)

    def spawn(self, argv, *, cwd=None, env=None, new_session: bool = True,
              own_group: bool = False) -> subprocess.Popen:
        """Start the child blocked on a gate, attach its pid, and release the gate only if attach succeeded.
        `new_session=False, own_group=True` (`gsched exec`): same session, but the child leads its own process group,
        so a group-wide signal aimed at the wrapper (Ctrl-C, a supervisor killing the group) is NOT also delivered
        to the child. The wrapper forwards each signal exactly once. Sharing the group made llama-server see SIGTERM
        twice ("Received second interrupt, terminating immediately") and abort in Metal teardown (live 2026-11-08)."""
        gate_r, gate_w = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, "-I", "-c", _BOOTSTRAP, str(gate_r), *argv], cwd=cwd, env=env,
                                     pass_fds=(gate_r,), start_new_session=new_session,
                                     process_group=0 if (own_group and not new_session) else None)
        finally:
            os.close(gate_r)
        try:
            self._call("attach", lease=self.token, pid=child.pid)
        except Exception:
            os.close(gate_w)             # closing without a byte makes the child exit 111 without exec
            child.wait()
            raise
        os.write(gate_w, b"1")
        os.close(gate_w)
        return child

    def finish(self, exit_code: int | None = None, signal: int | None = None) -> dict:
        params = {"lease": self.token}
        if exit_code is not None:
            params["exit_code"] = exit_code
        if signal is not None:
            params["signal"] = signal
        r = self._call("finish", **params)
        self.finished = True
        return r

    def release(self) -> dict:
        return self._call("release", lease=self.token)


@contextlib.contextmanager
def admitted(spec: dict, *, home=None, socket_override=None):
    kw = {"home": home, "socket_override": socket_override}
    spec = dict(spec, spawn="external")
    spec.setdefault("schema", "gpu-scheduler.job.v1")
    sub = client.result_or_raise(client.call("submit", {"spec": spec}, door="launcher", **kw))
    if not sub.get("job"):
        raise LauncherError(sub.get("hint") or "job was not queued")
    env = client.call_retry("wait", {"job": sub["job"], "until": "start"}, door="launcher", timeout=None, **kw)
    res = client.result_or_raise(env)
    if "lease" not in res:
        raise LauncherError(f"job {sub['job']} ended {res.get('state')} before it was admitted")
    lease = Lease(sub["job"], res["lease"], **kw)
    try:
        yield lease
    finally:
        try:
            lease.release()      # a no-op on terminal rows
        except Exception:
            pass
