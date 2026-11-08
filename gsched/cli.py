"""The `gsched` command line (09). Client side: imports only gsched.client and the stdlib at module level.

Exit codes: 0 ok; 2 refused / cancelled / ended not-done; 3 cannot connect; 4 request error; 64 usage;
75 another daemon is running; 78 configuration or platform error (daemon only); 124 timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time

EXIT_OK, EXIT_REFUSED, EXIT_CONNECT, EXIT_REQUEST, EXIT_USAGE, EXIT_TIMEOUT = 0, 2, 3, 4, 64, 124
TERMINAL = ("done", "failed", "cancelled", "lost")
LIVE_STATES = ["needs_estimate", "queued", "held"]


class UsageError(Exception):
    pass


# ---- parsing helpers -----------------------------------------------------------------------------------------
_DUR = re.compile(r"(\d+)\s*([dhms]?)")


def parse_duration(text: str) -> int:
    """90, 90s, 15m, 4h, 1d, 1h30m -> seconds."""
    t = str(text).strip().lower()
    if not t:
        raise UsageError("empty duration")
    if re.fullmatch(r"\d+", t):
        return int(t)
    if not re.fullmatch(r"(\d+[dhms])+", t):
        raise UsageError(f"bad duration {text!r}: use 90, 90s, 15m, 4h, 1d or 1h30m")
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)([dhms])", t))


def parse_mem(text: str) -> float:
    """20, 20G, 20GiB -> 20 (GiB); 512M, 512MiB -> 0.5. 'GB' is rejected."""
    t = str(text).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z]*)", t)
    if not m:
        raise UsageError(f"bad memory size {text!r}")
    n, unit = float(m.group(1)), m.group(2)
    u = unit.lower()
    if u in ("gb", "mb", "tb", "kb"):
        raise UsageError(f"{text!r}: memory is in GiB. Use {m.group(1)}G or {m.group(1)}GiB (decimal GB is not accepted)")
    if u in ("", "g", "gib"):
        return n
    if u in ("m", "mib"):
        return n / 1024
    if u in ("t", "tib"):
        return n * 1024
    raise UsageError(f"bad memory unit {unit!r} in {text!r}")


# ---- output ------------------------------------------------------------------------------------------------------
def emit(obj, args=None) -> None:
    compact = bool(getattr(args, "json", False)) or not sys.stdout.isatty()
    if compact:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()


def say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _fmt_g(v) -> str:
    return "-" if v is None else f"{v:.1f}G"


def print_table(jobs: list[dict]) -> None:
    hdr = f"{'ID':<22}{'PROJECT':<15}{'NAME':<13}{'STATE':<8}{'WHY/RUNNING':<16}{'MEM':<7}{'GPU':<6}{'PRI':>4}  Q"
    print(hdr)
    gpu = {"exclusive": "excl", "shared": "shr", "none": "none"}
    for j in jobs:
        if j["state"] == "held":
            started = j.get("started_ms")
            why = j.get("held_reason") or ""
            if started:
                why += " " + _short((time.time() * 1000 - started) / 1000)
        elif j.get("blocked_by"):
            why = ",".join(j["blocked_by"])
        else:
            why = j.get("end_reason") or (j.get("exit") or {}).get("end_reason") or ""
        res = j.get("resources", {})
        print(f"{j['job']:<22}{(j['project'] or '')[:14]:<15}{(j.get('name') or '')[:12]:<13}{j['state']:<8}"
              f"{why[:15]:<16}{_fmt_g(res.get('memory_gib')):<7}{gpu.get(res.get('gpu'), ''):<6}{j['priority']:>4}  "
              f"{j.get('queue') or ''}")


def _short(s: float) -> str:
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s % 60:02d}s"


# ---- RPC glue --------------------------------------------------------------------------------------------------------
def rpc(method: str, params: dict | None = None, *, timeout: float | None = 10.0, retry: bool = False,
        door: str = "cli"):
    """Returns (result_or_None, error_dict_or_None). Raises ConnectError."""
    from gsched import client
    fn = client.call_retry if retry else client.call
    kw = {"retry_s": 30.0} if retry else {}
    env = fn(method, params or {}, door=door, timeout=timeout, **kw)
    if env.get("ok"):
        return env["result"], None
    return None, env.get("error", {"code": "internal", "message": "malformed response"})


def fail(err: dict, args=None) -> int:
    from gsched.errors import EXIT_CODES
    emit({"error": err}, args)
    say(f"gsched: {err.get('code')}: {err.get('message')}")
    hint = (err.get("detail") or {}).get("hint")
    if hint:
        say(f"  {hint}")
    return EXIT_CODES.get(err.get("code", ""), EXIT_REQUEST)


def simple(method: str, params: dict, args, *, timeout=10.0) -> int:
    res, err = rpc(method, params, timeout=timeout)
    if err:
        return fail(err, args)
    emit(res, args)
    return EXIT_OK


# ---- commands -------------------------------------------------------------------------------------------------------------
def cmd_daemon(args) -> int:
    from gsched.daemon import run_daemon
    return run_daemon()


def _load_spec_arg(path: str) -> dict:
    data = sys.stdin.read() if path == "-" else open(path).read()
    try:
        return json.loads(data, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as e:
        raise UsageError(f"spec is not valid JSON: {e}")


def _submit(spec: dict, args) -> int:
    if spec.get("cwd") is None:
        spec["cwd"] = os.getcwd()
    spec.setdefault("schema", "gpu-scheduler.job.v1")
    res, err = rpc("submit", {"spec": spec})
    if err:
        return fail(err, args)
    if getattr(args, "print_id", False) and res.get("job"):
        print(res["job"])
    else:
        emit(res, args)
    if res.get("state") == "observe":
        say("gsched: " + res.get("hint", "observe mode: nothing was queued"))
    for w in res.get("warnings", []) or []:
        say(f"gsched: warning: {w}")
    if getattr(args, "wait", False) or getattr(args, "follow", False):
        if not res.get("job"):
            return EXIT_OK
        return _wait_for(res["job"], args, follow=getattr(args, "follow", False))
    return EXIT_OK


def cmd_submit(args) -> int:
    spec = _load_spec_arg(args.file)
    if not isinstance(spec, dict):
        raise UsageError("spec must be a JSON object")
    if args.after:
        spec["after"] = list(spec.get("after") or []) + list(args.after)
    if args.priority is not None:
        spec["priority"] = args.priority
    if args.queue:
        spec["queue"] = args.queue
    return _submit(spec, args)


def cmd_run(args) -> int:
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise UsageError("gsched run needs a command after `--`")
    project = args.project or os.environ.get("GSCHED_PROJECT") or _default_project()
    if not project:
        raise UsageError("no project: pass --project, set $GSCHED_PROJECT, or set default_project in the config")
    if args.mem is None:
        hint = ""
        try:
            res, err = rpc("suggest", {"project": project, "name": args.name})
            if res and res.get("memory_gib_p95"):
                hint = f" History for {project}/{args.name or '*'} suggests --mem {res['memory_gib_p95']:g}."
        except Exception:
            pass
        raise UsageError("--mem is required (GiB): there is no guessing." + hint)
    if args.checkpoint is None:
        raise UsageError("--checkpoint is required (atomic if the program cannot save itself)")
    env = {}
    for kv in args.env or []:
        if "=" not in kv:
            raise UsageError(f"--env expects K=V, got {kv!r}")
        k, v = kv.split("=", 1)
        env[k] = v
    spec = {"schema": "gpu-scheduler.job.v1", "project": project, "command": argv,
            "cwd": os.path.abspath(args.cwd) if args.cwd else os.getcwd(),
            "resources": {"memory_gib": parse_mem(args.mem), "gpu": args.gpu, "cpu_cores": args.cpu},
            "checkpoint": args.checkpoint, "priority": args.priority, "queue": args.queue}
    if args.name:
        spec["name"] = args.name
    if args.lock:
        spec["resources"]["locks"] = list(args.lock)
    rt = {}
    if args.est:
        rt["estimate_s"] = parse_duration(args.est)
    if args.limit:
        rt["limit_s"] = parse_duration(args.limit)
    if rt:
        spec["runtime"] = rt
    if args.after:
        spec["after"] = list(args.after)
    if args.allow_spill:
        spec["allow_spill"] = True
    if env:
        spec["env"] = env
    return _submit(spec, args)


def cmd_exec(args) -> int:
    if args.mem is None:
        raise UsageError("--mem is required (GiB): there is no guessing")
    if args.checkpoint is None:
        raise UsageError("--checkpoint is required (atomic if the program cannot save itself)")
    from gsched.exec_wrap import run_exec
    return run_exec(args)


def _default_project() -> str | None:
    try:
        from gsched.client import socket_path  # noqa: F401
        from gsched.config import Config
        from gsched.paths import Paths
        return Config.load(Paths.resolve().config).default_project
    except Exception:
        return None


def cmd_get(args) -> int:
    params = {"job": args.job}
    timeout = 10.0
    if args.wait:
        params["wait_s"] = parse_duration(args.wait)
        timeout = params["wait_s"] + 10
    res, err = rpc("get", params, timeout=timeout)
    if err:
        return fail(err, args)
    emit(res, args)
    return EXIT_OK


def cmd_list(args) -> int:
    params: dict = {"limit": args.limit}
    if args.state:
        params["state"] = args.state
    elif not args.all:
        params["state"] = LIVE_STATES
    if args.project:
        params["project"] = args.project
    res, err = rpc("list", params)
    if err:
        return fail(err, args)
    if sys.stdout.isatty() and not args.json:
        print_table(res["jobs"])
    else:
        emit(res, args)
    return EXIT_OK


def _wait_for(job: str, args, *, follow=False, until="exit", timeout_s: float | None = None) -> int:
    from gsched.client import ConnectError
    follow_thread = None
    if follow:
        # stream logs locally until the job is terminal, then return the wait code
        return _follow_logs(job, "stdout", wait_after=True, args=args)
    try:
        res, err = rpc("wait", {"job": job, "until": until}, timeout=timeout_s, retry=True)
    except socket.timeout:
        say(f"gsched: timed out waiting for {job}")
        return EXIT_TIMEOUT
    if err:
        return fail(err, args)
    emit(res, args)
    if until == "start":
        return EXIT_OK
    return EXIT_OK if res.get("state") == "done" else EXIT_REFUSED


def cmd_wait(args) -> int:
    t = parse_duration(args.timeout) if args.timeout else None
    return _wait_for(args.job, args, until=args.until, timeout_s=t)


def cmd_cancel(args) -> int:
    if not args.job and not args.lease:
        raise UsageError("cancel needs a job id or --lease")
    params = {"terminate": bool(args.terminate)}
    if args.lease:
        params["lease"] = args.lease
    else:
        params["job"] = args.job
    return simple("cancel", params, args)


def cmd_estimate(args) -> int:
    return simple("estimate", {"job": args.job, "estimate_s": parse_duration(args.duration)}, args)


def _follow_logs(job: str, stream: str, *, wait_after: bool, args, tail: int | None = None) -> int:
    offset = None
    first = True
    while True:
        params = {"job": job, "stream": stream}
        if offset is None:
            if tail:
                params["tail_bytes"] = tail
        else:
            params["offset"] = offset
            params["tail_bytes"] = 1024 * 1024
        res, err = rpc("logs", params, retry=True)
        if err:
            return fail(err, args)
        if res["data"]:
            sys.stdout.write(res["data"])
            sys.stdout.flush()
        offset = res["offset"] if not (offset is None and first and res["eof"] and not res["data"]) else res["offset"]
        first = False
        if res["eof"]:
            g, gerr = rpc("get", {"job": job}, retry=True)
            if gerr:
                return fail(gerr, args)
            if g["state"] in TERMINAL:
                # drain once more after the terminal state, then stop
                res2, _ = rpc("logs", {"job": job, "stream": stream, "offset": offset, "tail_bytes": 1024 * 1024})
                if res2 and res2["data"]:
                    sys.stdout.write(res2["data"])
                    sys.stdout.flush()
                if wait_after:
                    return EXIT_OK if g["state"] == "done" else EXIT_REFUSED
                return EXIT_OK
        time.sleep(0.5)


def cmd_logs(args) -> int:
    stream = "stderr" if args.stderr else "stdout"
    if args.follow:
        return _follow_logs(args.job, stream, wait_after=False, args=args, tail=args.tail)
    res, err = rpc("logs", {"job": args.job, "stream": stream, "tail_bytes": args.tail})
    if err:
        return fail(err, args)
    sys.stdout.write(res["data"])
    return EXIT_OK


def cmd_status(args) -> int:
    res, err = rpc("status", {"brief": bool(args.brief)})
    if err:
        return fail(err, args)
    if args.brief and not args.json:
        from gsched.status import brief_line
        print(brief_line(res))
    else:
        emit(res, args)
    return EXIT_OK


def cmd_profile_op(args) -> int:
    return simple(args.cmd, {"profile": args.profile}, args)


def cmd_drain(args) -> int:
    return simple(args.cmd, {}, args)


def cmd_attach(args) -> int:
    return simple("attach", {"lease": args.lease, "pid": args.pid}, args)


def cmd_finish(args) -> int:
    p = {"lease": args.lease}
    if args.exit_code is not None:
        p["exit_code"] = args.exit_code
    if args.signal is not None:
        p["signal"] = args.signal
    return simple("finish", p, args)


def cmd_release(args) -> int:
    return simple("release", {"lease": args.lease}, args)


def cmd_suggest(args) -> int:
    p = {"project": args.project}
    if args.name:
        p["name"] = args.name
    return simple("suggest", p, args)


def cmd_project(args) -> int:
    from gsched.paths import Paths, mkdir_private
    paths = Paths.resolve()
    if args.pcmd == "list":
        out = []
        try:
            for f in sorted(os.listdir(paths.projects)):
                if f.endswith(".json"):
                    try:
                        out.append(json.load(open(os.path.join(paths.projects, f))))
                    except ValueError:
                        out.append({"name": f[:-5], "error": "invalid JSON"})
        except FileNotFoundError:
            pass
        emit({"projects": out}, args)
        return EXIT_OK
    from gsched.projects import NAME_RE
    if not NAME_RE.match(args.name):
        raise UsageError("project names match ^[a-z0-9][a-z0-9._-]{0,63}$")
    mkdir_private(paths.projects)
    path = os.path.join(paths.projects, args.name + ".json")
    if os.path.exists(path) and not args.force:
        say(f"gsched: {path} exists; pass --force to overwrite")
        return EXIT_REFUSED
    body = {"name": args.name, "weight": args.weight, "mcp_submit": bool(args.mcp_submit), "mcp_terminate": False,
            "default_cwd": os.path.abspath(args.default_cwd) if args.default_cwd else None}
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(body, fh, indent=2)
        fh.write("\n")
    os.replace(path + ".tmp", path)
    _try_reload()
    emit({"project": body, "path": path}, args)
    return EXIT_OK


def _try_reload() -> None:
    try:
        rpc("reload", {}, timeout=3.0)
    except Exception:
        pass


def cmd_config(args) -> int:
    from gsched.config import Config, write_config_atomic
    from gsched.errors import ConfigError
    from gsched.paths import Paths
    paths = Paths.resolve()
    raw: dict = {}
    if os.path.exists(paths.config):
        try:
            raw = json.load(open(paths.config))
        except ValueError as e:
            say(f"gsched: {paths.config} is not valid JSON: {e}")
            return EXIT_REQUEST
    if args.ccmd == "get":
        try:
            eff = Config.load(paths.config).as_dict()
        except ConfigError as e:
            say(f"gsched: {e}")
            return 78
        if args.key:
            if args.key not in eff:
                say(f"gsched: unknown config key {args.key!r}")
                return EXIT_USAGE
            emit(eff[args.key], args)
        else:
            emit(eff, args)
        return EXIT_OK
    known = Config.defaults().as_dict()
    if args.key not in known:
        say(f"gsched: unknown config key {args.key!r}")
        return EXIT_USAGE
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    new = dict(raw)
    new[args.key] = value
    try:
        Config.from_dict(new)          # validate BEFORE writing
    except ConfigError as e:
        say(f"gsched: not written: {e}")
        return EXIT_REQUEST
    os.makedirs(paths.home, mode=0o700, exist_ok=True)
    write_config_atomic(paths.config, new)
    _try_reload()
    emit({args.key: value}, args)
    return EXIT_OK


def cmd_gc(args) -> int:
    p = {}
    if args.older_than:
        p["older_than_s"] = parse_duration(args.older_than)
    return simple("gc", p, args)


def cmd_version(args) -> int:
    from gsched import __version__
    out = {"version": __version__}
    try:
        res, err = rpc("ping", {}, timeout=2.0)
        if res:
            out["daemon"] = res
    except Exception:
        out["daemon"] = None
    emit(out, args)
    return EXIT_OK


def cmd_mcp(args) -> int:
    from gsched.mcp_stdio import main as mcp_main
    return mcp_main()


def cmd_install(args) -> int:
    from gsched.install import install
    return install(args)


def cmd_uninstall(args) -> int:
    from gsched.install import uninstall
    return uninstall(args)


def cmd_doctor(args) -> int:
    from gsched.doctor import run_doctor
    return run_doctor(args)


def cmd_dashboard(args) -> int:
    from gsched.dashboard import run_dashboard
    return run_dashboard(args)


# ---- parser --------------------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gsched", description="Local admission control for Apple silicon unified memory.")
    p.add_argument("--home", help="override GSCHED_HOME")
    p.add_argument("--socket", help="override the socket path")
    p.add_argument("--json", action="store_true", help="force compact JSON output")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("daemon", cmd_daemon, "run the daemon")
    sp.add_argument("--foreground", action="store_true", help="accepted for launchd; the daemon always runs in the foreground")
    sp = add("submit", cmd_submit, "submit a job spec")
    sp.add_argument("--file", "-f", required=True, help="spec file, or - for stdin")
    sp.add_argument("--after", action="append", default=[])
    sp.add_argument("--priority", type=int)
    sp.add_argument("--queue", choices=["auto", "day", "night"])
    sp.add_argument("--print-id", action="store_true")
    sp = add("run", cmd_run, "build a spec from flags and submit it")
    sp.add_argument("--print-id", action="store_true", help="print only the job id")
    sp.add_argument("--project")
    sp.add_argument("--name")
    sp.add_argument("--mem", help="memory reservation in GiB (required)")
    sp.add_argument("--gpu", choices=["exclusive", "shared", "none"], default="exclusive")
    sp.add_argument("--cpu", type=int, default=1)
    sp.add_argument("--lock", action="append")
    sp.add_argument("--est", help="estimated runtime, e.g. 3h")
    sp.add_argument("--limit", help="runtime limit")
    sp.add_argument("--checkpoint", choices=["atomic", "cooperative", "restartable"])
    sp.add_argument("--priority", type=int, default=0)
    sp.add_argument("--queue", choices=["auto", "day", "night"], default="auto")
    sp.add_argument("--after", action="append")
    sp.add_argument("--allow-spill", action="store_true")
    sp.add_argument("--env", action="append", metavar="K=V")
    sp.add_argument("--cwd")
    sp.add_argument("--wait", action="store_true")
    sp.add_argument("--follow", action="store_true")
    sp.add_argument("argv", nargs=argparse.REMAINDER, help="-- command ...")
    sp = add("exec", cmd_exec, "run a command as an external job (fail-open wrapper for LaunchAgents and cron)")
    sp.add_argument("--project")
    sp.add_argument("--name")
    sp.add_argument("--mem", help="memory reservation in GiB (required)")
    sp.add_argument("--gpu", choices=["exclusive", "shared", "none"], default="exclusive")
    sp.add_argument("--cpu", type=int, default=1)
    sp.add_argument("--lock", action="append")
    sp.add_argument("--checkpoint", choices=["atomic", "cooperative", "restartable"], help="required")
    sp.add_argument("--est", help="estimated runtime, e.g. 3h")
    sp.add_argument("--limit", help="runtime limit")
    sp.add_argument("--priority", type=int, default=0)
    sp.add_argument("--queue", choices=["auto", "day", "night"], default="auto")
    sp.add_argument("--max-wait", metavar="DUR", help="give up waiting for capacity after this long")
    sp.add_argument("--replace", action="store_true",
                    help="after admission, attach this process and exec the command in place (same pid/ancestry; "
                         "recommended for LaunchAgents: keeps TCC attribution and launchd signals; exit status is "
                         "not reported, so the job ends as lost/exit_unknown)")
    sp.add_argument("--connect-grace", metavar="DUR", default=None,
                    help="retry a missing/refused daemon socket this long before failing open (default 20s)")
    sp.add_argument("--on-max-wait", choices=["run", "fail"],
                    help="after --max-wait: run the command directly (default with fail-open) or exit 75")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--fail-open", action="store_true", help="exec the command directly if the scheduler is "
                      "unavailable or refuses (default)")
    mode.add_argument("--fail-closed", action="store_true", help="exit 75 instead of running unscheduled")
    sp.add_argument("argv", nargs=argparse.REMAINDER, help="-- command ...")
    sp = add("get", cmd_get, "show a job")
    sp.add_argument("job")
    sp.add_argument("--wait", metavar="DUR")
    sp = add("list", cmd_list, "list jobs")
    sp.add_argument("--state", action="append")
    sp.add_argument("--project")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--all", action="store_true", help="include finished jobs")
    sp = add("wait", cmd_wait, "wait for a job")
    sp.add_argument("job")
    sp.add_argument("--until", choices=["exit", "start"], default="exit")
    sp.add_argument("--timeout", metavar="DUR")
    sp = add("cancel", cmd_cancel, "cancel a job")
    sp.add_argument("job", nargs="?")
    sp.add_argument("--lease")
    sp.add_argument("--terminate", action="store_true", help="kill a running job (unsaved work is lost)")
    sp = add("estimate", cmd_estimate, "set a job's runtime estimate")
    sp.add_argument("job")
    sp.add_argument("duration")
    sp = add("logs", cmd_logs, "print a job's output")
    sp.add_argument("job")
    sp.add_argument("--stderr", action="store_true")
    sp.add_argument("--tail", type=int, default=8192, metavar="BYTES")
    sp.add_argument("--follow", "-f", action="store_true")
    sp = add("status", cmd_status, "capacity and queue")
    sp.add_argument("--brief", action="store_true")
    for name in ("ensure", "inhibit", "uninhibit", "hold", "unhold"):
        sp = add(name, cmd_profile_op, f"{name} a resident profile")
        sp.add_argument("profile")
    add("drain", cmd_drain, "stop admitting and unload idle residents")
    add("undrain", cmd_drain, "resume admitting")
    sp = add("attach", cmd_attach, "external launcher: attach a pid to a lease")
    sp.add_argument("--lease", required=True)
    sp.add_argument("--pid", type=int, required=True)
    sp = add("finish", cmd_finish, "external launcher: report the exit")
    sp.add_argument("--lease", required=True)
    sp.add_argument("--exit-code", type=int)
    sp.add_argument("--signal", type=int)
    sp = add("release", cmd_release, "external launcher: give the lease back")
    sp.add_argument("--lease", required=True)
    sp = add("suggest", cmd_suggest, "suggest resources from history")
    sp.add_argument("--project", required=True)
    sp.add_argument("--name")
    sp = add("project", cmd_project, "manage project files")
    psub = sp.add_subparsers(dest="pcmd", required=True)
    pa = psub.add_parser("add")
    pa.add_argument("name")
    pa.add_argument("--weight", type=int, default=1)
    pa.add_argument("--mcp-submit", action="store_true")
    pa.add_argument("--default-cwd")
    pa.add_argument("--force", action="store_true")
    psub.add_parser("list")
    sp = add("config", cmd_config, "get or set config keys")
    csub = sp.add_subparsers(dest="ccmd", required=True)
    cg = csub.add_parser("get")
    cg.add_argument("key", nargs="?")
    cs = csub.add_parser("set")
    cs.add_argument("key")
    cs.add_argument("value")
    sp = add("install", cmd_install, "install the daemon as a LaunchAgent")
    sp.add_argument("--python")
    sp.add_argument("--no-load", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--venv-symlinks", action="store_true",
                    help="create the venv with symlinks so the daemon runs the same interpreter binary as --python "
                         "(default: --copies)")
    sp.add_argument("--plist-dir", help="LaunchAgents directory (default ~/Library/LaunchAgents)")
    sp.add_argument("--dashboard", action="store_true", help="also install the dashboard LaunchAgent")
    sp.add_argument("--dashboard-port", type=int, default=8765)
    sp = add("uninstall", cmd_uninstall, "remove the LaunchAgent(s)")
    sp.add_argument("--keep-data", action="store_true")
    sp.add_argument("--purge", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-load", action="store_true", help="do not call launchctl")
    sp.add_argument("--plist-dir", help="LaunchAgents directory (default ~/Library/LaunchAgents)")
    sp = add("doctor", cmd_doctor, "check the environment")
    sp.add_argument("--json", dest="doctor_json", action="store_true")
    sp = add("gc", cmd_gc, "apply retention now")
    sp.add_argument("--older-than", metavar="DUR")
    add("mcp", cmd_mcp, "MCP stdio server")
    add("version", cmd_version, "print the version")
    sp = add("dashboard", cmd_dashboard, "read-only web dashboard (loopback, token protected)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--open", action="store_true")
    sp.add_argument("--print-url", action="store_true")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        # `run` takes everything after the options as the command; make `--` optional for the REMAINDER
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code not in (0, None) else 0
    if args.home:
        os.environ["GSCHED_HOME"] = args.home
    if args.socket:
        os.environ["GSCHED_SOCKET"] = args.socket
    if not getattr(args, "fn", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return args.fn(args)
    except UsageError as e:
        say(f"gsched: {e}")
        return EXIT_USAGE
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        from gsched.client import ConnectError, ProtocolError
        if isinstance(e, ConnectError):
            say(f"gsched: {e}")
            return EXIT_CONNECT
        if isinstance(e, (ProtocolError, ConnectionError)):
            say(f"gsched: connection problem: {e}")
            return EXIT_CONNECT
        if isinstance(e, FileNotFoundError):
            say(f"gsched: {e}")
            return EXIT_USAGE
        raise


if __name__ == "__main__":
    sys.exit(main())
