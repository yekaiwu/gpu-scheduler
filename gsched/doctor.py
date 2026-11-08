"""`gsched doctor` (11 section 4). Local checks run in the CLI process; daemon checks run INSIDE the daemon
process via the `doctor` RPC, because macOS TCC applies per process and only the daemon's view matters for jobs."""

from __future__ import annotations

import json
import os
import platform as pyplatform
import re
import shutil
import stat
import subprocess
import sys

from gsched import __version__
from gsched.units import GiB

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


def chk(cid: int, name: str, level: str, message: str, fix: str = "", where: str = "daemon") -> dict:
    return {"id": cid, "name": name, "level": level, "message": message, "fix": fix, "where": where}


# ---- daemon-side checks ------------------------------------------------------------------------------------------------
def daemon_checks(d, listdir=os.listdir, home: str | None = None) -> list[dict]:
    out: list[dict] = []
    cfg = d.cfg
    label = cfg.launchd_label
    # 4 layout
    try:
        rep = d.platform.layout_report()
        out.append(chk(4, "layout self-check", PASS if rep.get("ok") else FAIL, "ctypes layouts verified",
                       "run tools/check_layouts.c and compare with docs/implementation/02-platform.md"))
    except Exception as e:
        out.append(chk(4, "layout self-check", FAIL, str(e), "see tools/check_layouts.c"))
    # 5 memory probe
    s = d.platform.mem_sample(d.clock.now_ms())
    mine = s.reclaimable_bytes
    ref = _vm_stat_reclaimable()
    if ref is None:
        out.append(chk(5, "memory probe", INFO, f"reclaimable {mine / GiB:.1f} GiB (vm_stat not available to compare)"))
    else:
        err = abs(ref - mine) / max(ref, 1)
        out.append(chk(5, "memory probe", PASS if err < 0.05 else FAIL,
                       f"reclaimable {mine / GiB:.1f} GiB vs vm_stat {ref / GiB:.1f} GiB ({err * 100:.1f}% apart)",
                       "re-run; if it persists, check the vm_statistics64 offsets with tools/check_layouts.c"))
    # 6 swap / level
    problems = []
    if s.swap_used_bytes is None:
        problems.append("swap usage unreadable")
    if s.level is None:
        problems.append("memorystatus_level unreadable")
    out.append(chk(6, "swap and pressure level", FAIL if "swap usage unreadable" in problems else
                   (WARN if problems else PASS), ", ".join(problems) or f"swap {((s.swap_used_bytes or 0) / GiB):.2f} GiB, level {s.level}",
                   "admissions are paused while swap is unreadable (fail-closed)"))
    # 7 TCC. The daemon NEVER lists a protected folder in-process: under launchd that call blocks forever behind a
    # consent prompt. State comes from the guard's probe subprocesses (gsched/tcc.py); `home`/`listdir` are kept for
    # callers but unused for protected roots.
    exe = os.path.realpath(sys.executable)
    home_ = d.tcc.home
    for e in d.tcc.report():
        root = e["root"]
        shown = "~" + root[len(home_):] if root.startswith(home_ + "/") else root
        st = e["state"]
        if st == "unknown":
            d.tcc.ensure_probe(root)
            out.append(chk(7, f"TCC: {shown}", INFO, "probe running; run doctor again in a few seconds"))
        elif st == "ok":
            out.append(chk(7, f"TCC: {shown}", PASS, "readable by the daemon (probed in a subprocess)"))
        elif st == "absent":
            out.append(chk(7, f"TCC: {shown}", PASS, "does not exist"))
        else:
            what = ("the probe got no answer within 3 s: macOS is waiting on a consent prompt, or it was denied"
                    if st == "blocked" else "macOS refused access (EPERM)")
            out.append(chk(7, f"TCC: {shown}", WARN,
                           f"The daemon cannot read {shown}: {what}. Jobs whose cwd or files are there will be refused "
                           "at submit.",
                           "Fix: System Settings -> Privacy & Security -> Full Disk Access -> + -> press Cmd+Shift+G and "
                           f"paste {exe} -> enable -> `launchctl kickstart -k gui/$UID/{label}`. "
                           "Alternative: keep job code outside protected folders."))
    # 18 which binary needs Full Disk Access
    real = os.path.realpath(sys.executable)
    linked = real != os.path.abspath(sys.executable)
    out.append(chk(18, "daemon interpreter", INFO,
                   f"{sys.executable}" + (f" is a symlink to {real}: grant Full Disk Access to {real}" if linked
                                          else " (a private copy: grant Full Disk Access to this binary)")))
    # 8 default_cwd
    for p in d.projects.all():
        if p.default_cwd:
            root = d.tcc.root_for(p.default_cwd)
            if root is not None:
                st = d.tcc.state(root)
                level = PASS if st in ("ok", "absent") else (INFO if st == "unknown" else WARN)
                out.append(chk(8, f"project {p.name} default_cwd", level, f"{p.default_cwd}: {root} is {st} (cached probe)",
                               "" if level != WARN else "grant Full Disk Access to the daemon's Python or move the directory"))
                continue
            try:
                listdir(p.default_cwd)
                out.append(chk(8, f"project {p.name} default_cwd", PASS, p.default_cwd))
            except OSError as e:
                out.append(chk(8, f"project {p.name} default_cwd", WARN, f"{p.default_cwd}: {e}",
                               "grant Full Disk Access to the daemon's Python or move the directory"))
    # 9 profiles
    reg = d.residents.registry
    if reg is not None:
        from gsched.checks import resolve_command
        for name, prof in reg.profiles.items():
            try:
                resolve_command(prof.spec.command, prof.spec.env.get("PATH") or cfg.base_path, prof.spec.cwd)
                out.append(chk(9, f"profile {name}", PASS, "loads; command resolves"))
            except Exception as e:
                out.append(chk(9, f"profile {name}", FAIL, str(getattr(e, "message", e)), "fix the profile file"))
        for name, err in reg.errors.items():
            out.append(chk(9, f"profile {name}", FAIL, err, "fix the profile file"))
    # 10 wired limit
    m = d.machine
    lim = m.gpu_wired_limit_mb
    msg = (f"iogpu.wired_limit_mb = {lim}" + (" (macOS default: about 2/3-3/4 of RAM can be GPU-wired)" if lim == 0 else "")
           if lim is not None else "iogpu.wired_limit_mb is not present on this macOS")
    big = [r["id"] for r in d.store.live_rows() if r["gpu"] != "none" and r["memory_bytes"] > 0.75 * m.memsize_bytes]
    level = WARN if big else INFO
    out.append(chk(10, "GPU wired limit", level, msg + (f"; queued/running jobs ask for > 75% of RAM: {big}" if big else ""),
                   "Large models may need `sudo sysctl iogpu.wired_limit_mb=<N>` (not persistent across reboot). gsched never changes it."))
    # 11 budget
    out.append(chk(11, "memory budget", INFO,
                   f"memsize {m.memsize_bytes / GiB:.1f} GiB, os_reserve {(m.memsize_bytes - d.budget_bytes) / GiB:.1f} GiB, "
                   f"budget {d.budget_bytes / GiB:.1f} GiB, headroom {cfg.headroom_gib:g} GiB"))
    # 12 foreign heavy
    heavy = list(d.protective.reasons)
    out.append(chk(12, "foreign heavy processes", INFO,
                   ("none right now" if not heavy else "protective mode is ON: " +
                    ", ".join(f"{o.path or 'port ' + str(o.port)} (pid {o.pid})" for o in heavy))))
    # 13 enforce
    out.append(chk(13, "enforce", INFO, "enforcing" if cfg.enforce else
                   "observe mode: nothing is scheduled. `gsched config set enforce true`"))
    # 15 caffeinate
    out.append(chk(15, "caffeinate", PASS if os.path.exists("/usr/bin/caffeinate") else WARN,
                   "/usr/bin/caffeinate present" if os.path.exists("/usr/bin/caffeinate") else "caffeinate is missing",
                   "overnight jobs will not hold the Mac awake"))
    # 16 low power mode
    lp = _low_power_mode()
    if lp:
        out.append(chk(16, "Low Power Mode", WARN, "Low Power Mode is on: jobs will run slower",
                       "turn it off in System Settings -> Battery"))
    else:
        out.append(chk(16, "Low Power Mode", PASS, "off or not applicable"))
    return out


def _vm_stat_reclaimable() -> int | None:
    if not os.path.exists("/usr/bin/vm_stat"):
        return None
    try:
        txt = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=3).stdout
        page = int(re.search(r"page size of (\d+) bytes", txt).group(1))
        pg = lambda label: int(re.search(re.escape(label) + r":\s+(\d+)\.", txt).group(1))
        return (pg("Pages free") + pg("Pages purgeable") + pg("File-backed pages")) * page
    except Exception:
        return None


def _low_power_mode() -> bool:
    try:
        txt = subprocess.run(["/usr/bin/pmset", "-g"], capture_output=True, text=True, timeout=3).stdout
        return bool(re.search(r"lowpowermode\s+1", txt))
    except Exception:
        return False


# ---- local checks ------------------------------------------------------------------------------------------------------------
def local_checks() -> list[dict]:
    from gsched import client
    from gsched.config import Config
    from gsched.paths import Paths, SUN_PATH_MAX
    out: list[dict] = []
    paths = Paths.resolve()
    # 1 platform
    problems = []
    if sys.platform != "darwin":
        problems.append("not macOS")
    if pyplatform.machine() != "arm64":
        problems.append(f"Python reports {pyplatform.machine()} (use a native arm64 Python; Rosetta is refused)")
    if sys.version_info < (3, 12):
        problems.append(f"Python {sys.version_info.major}.{sys.version_info.minor} < 3.12")
    try:
        if sys.platform == "darwin" and int(pyplatform.mac_ver()[0].split(".")[0]) < 14:
            problems.append(f"macOS {pyplatform.mac_ver()[0]} < 14")
    except ValueError:
        pass
    out.append(chk(1, "platform", FAIL if problems else PASS,
                   "; ".join(problems) or f"Apple silicon, Python {sys.version_info.major}.{sys.version_info.minor}, "
                   f"macOS {pyplatform.mac_ver()[0]}", "gsched needs macOS 14+ on Apple silicon with a native Python 3.12+",
                   "local"))
    # 2, 3 daemon reachable / version
    cfg = Config.load(paths.config) if os.path.exists(paths.config) else Config.defaults()
    sock = client.socket_path()
    ping = None
    try:
        env = client.call("ping", {}, timeout=3)
        ping = env.get("result") if env.get("ok") else None
    except Exception:
        ping = None
    if ping:
        out.append(chk(2, "daemon reachable", PASS, f"pid {ping['pid']}, uptime {ping['uptime_s']}s", where="local"))
        if ping["version"] != __version__:
            out.append(chk(3, "daemon version", WARN, f"daemon {ping['version']} != CLI {__version__}",
                           "re-run `gsched install` to update the daemon copy", "local"))
        else:
            out.append(chk(3, "daemon version", PASS, __version__, where="local"))
    else:
        out.append(chk(2, "daemon reachable", FAIL, f"not running (socket {sock})",
                       f"launchctl bootstrap gui/$UID ~/Library/LaunchAgents/{cfg.launchd_label}.plist ; "
                       f"see {paths.p('logs', 'launchd.err.log')}", "local"))
    # 14 socket path / modes
    probs = []
    if len(os.fsencode(sock)) >= SUN_PATH_MAX:
        probs.append(f"socket path is {len(os.fsencode(sock))} bytes (limit {SUN_PATH_MAX - 1})")
    for label, path, want in (("run dir", paths.run, 0o700), ("socket", sock, 0o600), ("database", paths.db, 0o600)):
        if os.path.exists(path):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode != want:
                probs.append(f"{label} mode is {oct(mode)}, want {oct(want)}")
    out.append(chk(14, "socket path and modes", FAIL if probs else PASS, "; ".join(probs) or "ok",
                   "chmod the file, or set a shorter GSCHED_HOME / socket_path", "local"))
    # 17 PATH
    found = shutil.which("gsched")
    mine = paths.p("bin", "gsched")
    if found and os.path.realpath(found) == os.path.realpath(mine):
        out.append(chk(17, "gsched on PATH", PASS, found, where="local"))
    elif found:
        out.append(chk(17, "gsched on PATH", WARN, f"{found} is not {mine}; it may be a different version",
                       f'prefer: export PATH="{paths.bin}:$PATH"', "local"))
    else:
        out.append(chk(17, "gsched on PATH", WARN, "gsched is not on PATH", f'export PATH="{paths.bin}:$PATH"', "local"))
    return out


def run_doctor(args) -> int:
    from gsched import client
    checks = local_checks()
    daemon_part: list[dict] = []
    try:
        env = client.call("doctor", {}, timeout=30)
        if env.get("ok"):
            daemon_part = env["result"]["checks"]
        else:
            checks.append(chk(0, "daemon checks", FAIL, env.get("error", {}).get("message", "error"), where="local"))
    except Exception:
        pass
    allc = sorted(checks + daemon_part, key=lambda c: (c["id"], c["name"]))
    if getattr(args, "doctor_json", False) or getattr(args, "json", False):
        print(json.dumps({"checks": allc}))
    else:
        for c in allc:
            print(f"{c['level']:<5} {c['id']:>2} {c['name']}: {c['message']}")
            if c["fix"] and c["level"] in (WARN, FAIL):
                print(f"          fix: {c['fix']}")
    return 2 if any(c["level"] == FAIL for c in allc) else 0
