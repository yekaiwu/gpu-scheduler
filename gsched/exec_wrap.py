"""`gsched exec`: wrap any existing command as an external job (docs/implementation/16 section 1).

Meant to be put in front of an existing LaunchAgent's ProgramArguments or a cron line without changing the program.
Fail-open by default: if the scheduler is unreachable, in observe mode, or rejects the request, the command is exec'd
directly so the process tree is exactly what it was without the wrapper.

Client side only (no daemon imports). Signals reach the wrapped command only through gsched.platform.darwin helpers.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time

EXIT_REFUSED, EXIT_TEMPFAIL = 2, 75
_FORWARDED = ("SIGTERM", "SIGINT", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGQUIT")
_TERMINATE_CODES = {signal.SIGTERM: 143, signal.SIGINT: 130, signal.SIGHUP: 129}
FINISH_TRIES = 3
WAIT_RETRY_S = 30.0


class _Terminate(BaseException):
    """Raised from the signal handler while we are queued: cancel and leave without running the command."""

    def __init__(self, code: int):
        self.code = code


DEFAULT_CONNECT_GRACE_S = 20.0

def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_spec(args, project: str, argv: list[str]) -> dict:
    """Same shape as `gsched run`, with spawn=external. The environment is NOT sent: the child inherits ours."""
    from gsched.cli import UsageError, parse_duration, parse_mem
    res = {"memory_gib": parse_mem(args.mem), "gpu": args.gpu, "cpu_cores": args.cpu}
    if args.lock:
        res["locks"] = list(args.lock)
    spec = {"schema": "gpu-scheduler.job.v1", "project": project, "name": args.name or os.path.basename(argv[0]),
            "command": argv, "cwd": os.getcwd(), "spawn": "external", "resources": res,
            "checkpoint": args.checkpoint, "priority": args.priority, "queue": args.queue}
    rt = {}
    if args.est:
        rt["estimate_s"] = parse_duration(args.est)
    elif args.checkpoint != "restartable":
        # atomic/cooperative jobs without an estimate would park in needs_estimate until a human runs `gsched estimate`,
        # and a wrapper has no human. Use the configured default for unknown runtimes (docs 16 section 2).
        rt["estimate_s"] = _default_unknown_estimate()
    if args.limit:
        rt["limit_s"] = parse_duration(args.limit)
    if rt:
        spec["runtime"] = rt
    if args.max_wait:
        parse_duration(args.max_wait)       # validate early
    if not argv:
        raise UsageError("gsched exec needs a command after `--`")
    return spec


def _default_unknown_estimate() -> int:
    try:
        from gsched.config import Config
        from gsched.paths import Paths
        return Config.load(Paths.resolve().config).default_unknown_estimate_s
    except Exception:
        return 7200


def _fallback_exec(argv: list[str], why: str) -> int:
    say(f"gsched: exec fallback: {why}")
    return _exec(argv)


def _exec(argv: list[str]) -> int:
    """Replace this process. The process tree is then identical to running without the wrapper."""
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        say(f"gsched: cannot exec {argv[0]}: {e}")
        return 126 if isinstance(e, PermissionError) else 127
    return 0   # unreachable


def run_exec(args) -> int:
    from gsched import client
    from gsched.cli import UsageError, parse_duration
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise UsageError("gsched exec needs a command after `--`")
    project = args.project or os.environ.get("GSCHED_PROJECT") or _project_from_config()
    if not project:
        raise UsageError("no project: pass --project, set $GSCHED_PROJECT, or set default_project in the config")
    spec = build_spec(args, project, argv)
    fail_open = not args.fail_closed
    max_wait = parse_duration(args.max_wait) if args.max_wait else None
    on_max = args.on_max_wait or ("run" if fail_open else "fail")

    def unavailable(why: str) -> int:
        if fail_open:
            return _fallback_exec(argv, why)
        say(f"gsched: scheduler unavailable ({why}); --fail-closed, not running the command")
        return EXIT_TEMPFAIL

    # ---- 2. submit ----------------------------------------------------------------------------------------------
    # A daemon that is (re)starting has no socket for a moment: launchd starts agents at login in no particular
    # order, and KeepAlive restarts take ThrottleInterval. Retry a refused/missing socket for --connect-grace
    # before failing open, so a resident started alongside the daemon is not run unscheduled (live 2026-11-08).
    grace = parse_duration(args.connect_grace) if getattr(args, "connect_grace", None) else DEFAULT_CONNECT_GRACE_S
    connect_deadline = time.monotonic() + grace
    while True:
        try:
            env = client.call("submit", {"spec": spec}, door="launcher", timeout=10.0)
            break
        except client.ConnectError as e:
            if time.monotonic() >= connect_deadline:
                return unavailable(f"daemon unreachable after {grace:g}s: {e}")
            time.sleep(0.5)
        except (client.ProtocolError, OSError, socket.timeout) as e:
            return unavailable(f"daemon unreachable: {e}")
    if not env.get("ok"):
        err = env.get("error", {})
        reason = f"{err.get('code')}: {err.get('message')}"
        say(f"gsched: {reason}")
        if fail_open:
            return _fallback_exec(argv, reason)
        return EXIT_REFUSED
    sub = env["result"]
    if sub.get("state") == "observe":
        return unavailable("the scheduler is in observe mode (enforce=false)")
    job = sub.get("job")
    if not job:
        return unavailable("the scheduler did not queue the job")
    for w in sub.get("warnings") or []:
        say(f"gsched: warning: {w}")

    # ---- 3/4. park on the start, cancel on SIGTERM/INT/HUP and on --max-wait --------------------------------------
    def on_term(signum, frame):
        raise _Terminate(_TERMINATE_CODES.get(signum, 128 + signum))

    restore = _install(_TERMINATE_CODES, on_term)
    try:
        lease, rc = _wait_for_start(client, job, max_wait, on_max, fail_open, argv)
    except _Terminate as t:
        _restore(restore)
        _cancel(client, job)
        say(f"gsched: terminated while queued; job {job} cancelled, the command was not run")
        return t.code
    if rc is not None:
        _restore(restore)      # about to exec (or leave): a signal disposition we changed must not leak into the command
        return rc

    # ---- 5r. --replace: attach OUR pid, then exec the command in place --------------------------------------------
    # The process launchd (or cron) started becomes the command itself, exactly as without the wrapper: same pid,
    # same ancestry, same TCC responsible process (live 2026-11-08: a wrapper-parent made a LaunchAgent's
    # Documents-resident dylibs/venv hang on a TCC prompt), and launchd's SIGTERM goes straight to the program.
    # Cost: nobody reports the exit status, so the daemon ends the job as `lost` (exit_unknown) when the pid goes.
    if getattr(args, "replace", False):
        _restore(restore)
        from gsched import launcher
        ls = launcher.Lease(job, lease)
        try:
            ls._call("attach", lease=lease, pid=os.getpid())
        except Exception as e:
            _release(ls)
            return unavailable(f"attach failed: {e}")
        return _exec(argv)

    # ---- 5. got the lease: gated child, attach, release -----------------------------------------------------------
    from gsched import launcher
    from gsched.platform import darwin
    ls = launcher.Lease(job, lease)
    state = {"child": None, "pending": []}

    def forward(signum, frame):
        if state["child"] is None:
            state["pending"].append(signum)     # delivered right after the gate opens: the command still sees it
        else:
            darwin.signal_own_child(state["child"], signum)

    _restore(restore)
    restore = _install([getattr(signal, n) for n in _FORWARDED], forward)
    try:
        child = ls.spawn(argv, new_session=False, own_group=True)   # same session, own group: signals forwarded once
    except Exception as e:
        _release(ls)
        _restore(restore)
        return unavailable(f"attach failed: {e}")
    state["child"] = child
    for signum in state["pending"]:
        darwin.signal_own_child(child, signum)
    # ---- 6. wait for the exact status -----------------------------------------------------------------------------
    rc = child.wait()
    # ---- 7. finish (3 tries), then exit with the child's exact fate -----------------------------------------------
    code = rc if rc >= 0 else None
    sig = -rc if rc < 0 else None
    ok = False
    for i in range(FINISH_TRIES):
        try:
            ls.finish(exit_code=code, signal=sig)
            ok = True
            break
        except Exception:
            time.sleep(0.2 * (i + 1))
    if not ok:
        say(f"gsched: could not report the exit of job {job} to the daemon; it will be marked lost")
    if sig is not None:
        darwin.exit_by_signal(sig)
        return 128 + sig
    return code


def _install(sigs, handler) -> dict:
    """Install `handler` for each signal that is not being ignored (a nohup'd SIGHUP must stay ignored)."""
    old = {}
    for s in sigs:
        cur = signal.getsignal(s)
        if cur == signal.SIG_IGN:
            continue
        old[s] = cur
        signal.signal(s, handler)
    return old


def _restore(old: dict) -> None:
    for s, h in old.items():
        signal.signal(s, h)


def _wait_for_start(client, job: str, max_wait, on_max: str, fail_open: bool, argv: list[str]):
    """Returns (lease, None) when admitted, or (None, exit_code) when the wrapper must leave."""
    start = time.monotonic()
    deadline = None if max_wait is None else start + max_wait
    retry_until = None
    while True:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining == 0.0:
            return None, _max_wait_expired(client, job, on_max, fail_open, argv)
        try:
            env = client.call("wait", {"job": job, "until": "start"}, door="launcher", timeout=remaining)
        except socket.timeout:
            return None, _max_wait_expired(client, job, on_max, fail_open, argv)
        except (client.ConnectError, client.ProtocolError, ConnectionError, OSError):
            # daemon restarted or went away: the queued row survives, only our parked connection is gone. Re-park.
            retry_until = retry_until or time.monotonic() + WAIT_RETRY_S
            if time.monotonic() >= retry_until:
                if fail_open:
                    return None, _fallback_exec(argv, "the daemon went away while this job was queued")
                say("gsched: the daemon went away while queued; --fail-closed, not running the command")
                return None, EXIT_TEMPFAIL
            time.sleep(0.5)
            continue
        retry_until = None
        if not env.get("ok"):
            err = env.get("error", {})
            reason = f"{err.get('code')}: {err.get('message')}"
            if fail_open:
                return None, _fallback_exec(argv, reason)
            say(f"gsched: {reason}")
            return None, EXIT_REFUSED
        res = env["result"]
        if res.get("lease"):
            return res["lease"], None
        # no lease: the job ended before it was admitted (cancelled by someone, dependency, waiter_gone...)
        say(f"gsched: job {job} ended {res.get('state')} before it was admitted "
            f"({(res.get('exit') or {}).get('end_reason')}); the command was not run")
        return None, EXIT_REFUSED


def _max_wait_expired(client, job: str, on_max: str, fail_open: bool, argv: list[str]) -> int:
    _cancel(client, job, end_reason="max_wait")
    if on_max == "run":
        return _fallback_exec(argv, f"--max-wait expired for job {job}; cancelled it and running the command directly")
    say(f"gsched: --max-wait expired for job {job}; cancelled it; not running the command (--on-max-wait fail)")
    return EXIT_TEMPFAIL


def _cancel(client, job: str, end_reason: str | None = None) -> None:
    p = {"job": job}
    if end_reason:
        p["end_reason"] = end_reason
    try:
        client.call("cancel", p, door="launcher", timeout=3.0)
    except Exception:
        pass


def _release(ls) -> None:
    try:
        ls.release()
    except Exception:
        pass


def _project_from_config() -> str | None:
    try:
        from gsched.config import Config
        from gsched.paths import Paths
        return Config.load(Paths.resolve().config).default_project
    except Exception:
        return None
