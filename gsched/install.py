"""`gsched install` / `gsched uninstall` (11 sections 2-3).

The LaunchAgent never runs code from a checkout (which may sit under a TCC-protected folder) or from a tool venv that
uv/pipx may replace: install copies the package into $GSCHED_HOME/app and creates a private venv.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from xml.sax.saxutils import escape

from gsched import __version__, templates
from gsched.config import Config, write_config_atomic
from gsched.paths import Paths, mkdir_private

DEFAULT_PLIST_DIR = "~/Library/LaunchAgents"


def say(msg: str) -> None:
    print(msg, file=sys.stderr)


class InstallError(Exception):
    pass


def plist_dir(args) -> str:
    return os.path.expanduser(getattr(args, "plist_dir", None) or os.environ.get("GSCHED_LAUNCHAGENTS_DIR")
                              or DEFAULT_PLIST_DIR)


def check_interpreter(py: str) -> None:
    try:
        out = subprocess.run([py, "-c", "import sys,platform; print(sys.version_info[:2], platform.machine())"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise InstallError(f"cannot run {py}: {e}")
    txt = out.stdout.strip()
    try:
        ver, machine = txt.rsplit(")", 1)
        major, minor = (int(x) for x in ver.lstrip("(").split(","))
        machine = machine.strip()
    except ValueError:
        raise InstallError(f"{py} did not report a usable version ({txt!r}; {out.stderr.strip()[:200]})")
    if (major, minor) < (3, 12):
        hint = "Install 3.12+: `brew install python@3.12` or `uv python install 3.12`."
        sys_note = " (macOS system Python)" if py.startswith("/usr/bin/") else ""
        raise InstallError(f"{py} is {major}.{minor}{sys_note}. {hint}")
    if machine != "arm64":
        raise InstallError(f"{py} runs as {machine}, not arm64 (Rosetta?). Use a native arm64 Python.")


def render_plist(template: str, **values) -> str:
    text = template
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", escape(str(v)))
    plistlib.loads(text.encode())          # must parse
    return text


def _run(cmd: list[str], check=False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _label(paths: Paths) -> str:
    try:
        return Config.load(paths.config).launchd_label
    except Exception:
        return Config.defaults().launchd_label


def install(args) -> int:
    paths = Paths.resolve()
    home = paths.home
    py = args.python or sys.executable
    try:
        check_interpreter(py)
    except InstallError as e:
        say(f"gsched install: {e}")
        return 78
    created = {}
    # 2. home
    paths.ensure_dirs()
    for d in (paths.bin, paths.p("rollback")):
        mkdir_private(d)
    os.chmod(paths.bin, 0o755)
    # 3. venv
    venv_py = os.path.join(paths.venv, "bin", "python3")
    want_links = bool(getattr(args, "venv_symlinks", False))
    if want_links != os.path.islink(venv_py) and os.path.lexists(venv_py):
        shutil.rmtree(paths.venv, ignore_errors=True)       # switching between --copies and --venv-symlinks rebuilds
    if not (os.path.exists(venv_py) and _run([venv_py, "-c", "import sys; print(sys.version)"]).returncode == 0):
        # --venv-symlinks: the daemon runs the SAME interpreter binary as --python (use it when that binary already has
        # Full Disk Access). The default --copies gives a stable binary of its own to grant access to.
        r = _run([py, "-m", "venv", "--symlinks" if want_links else "--copies", "--without-pip", paths.venv])
        if r.returncode != 0:
            say("gsched install: venv --copies failed; falling back to symlinks. "
                "Full Disk Access must then be granted to the realpath of the venv python and re-granted after Python upgrades.")
            shutil.rmtree(paths.venv, ignore_errors=True)
            r = _run([py, "-m", "venv", "--without-pip", paths.venv])
            if r.returncode != 0:
                say(f"gsched install: cannot create the venv: {r.stderr.strip()[:300]}")
                return 78
        if _run([venv_py, "-c", "import sys; print(sys.version)"]).returncode != 0:
            say(f"gsched install: the new venv interpreter does not run: {venv_py}")
            return 78
    created["venv_python"] = venv_py
    created["venv_python_realpath"] = os.path.realpath(venv_py)
    created["venv_mode"] = "symlinks" if want_links else "copies"
    if want_links:
        say(f"gsched install: --venv-symlinks: the daemon runs {os.path.realpath(venv_py)}. Full Disk Access must be "
            "granted to that binary (and re-granted after a Python upgrade). `gsched doctor` prints it.")
    # 4. app copy (atomic rename; never a symlink)
    pkg_dir = os.path.dirname(os.path.abspath(sys.modules["gsched"].__file__))
    new_app, old_app = paths.home + "/app.new", paths.home + "/app.old"
    shutil.rmtree(new_app, ignore_errors=True)
    os.makedirs(new_app, mode=0o700)
    shutil.copytree(pkg_dir, os.path.join(new_app, "gsched"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(old_app, ignore_errors=True)
    if os.path.exists(paths.app):
        os.replace(paths.app, old_app)
    os.replace(new_app, paths.app)
    shutil.rmtree(old_app, ignore_errors=True)
    site = _run([venv_py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"]).stdout.strip()
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "gsched-app.pth"), "w") as fh:
        fh.write(paths.app + "\n")
    # 5. wrapper
    wrapper = paths.p("bin", "gsched")
    with open(wrapper, "w") as fh:
        fh.write(templates.WRAPPER.replace("{{VENV_PYTHON}}", venv_py))
    os.chmod(wrapper, 0o755)
    v = subprocess.run([wrapper, "version"], capture_output=True, text=True, env={**os.environ, "GSCHED_HOME": home})
    if v.returncode != 0:
        say(f"gsched install: the installed wrapper failed to run: {v.stderr.strip()[:300]}")
        return 78
    # 6. config (never overwritten)
    new_config = not os.path.exists(paths.config)
    if new_config:
        raw = {"enforce": False,
               "_comment": "Observe mode: nothing is scheduled. See docs/implementation/01-repo-and-conventions.md "
                           "for every key; enable with `gsched config set enforce true`."}
        # 7. example project
        if not os.listdir(paths.projects):
            with open(os.path.join(paths.projects, "default.json"), "w") as fh:
                json.dump({"name": "default", "weight": 1}, fh)
            os.chmod(os.path.join(paths.projects, "default.json"), 0o600)
            raw["default_project"] = "default"
        write_config_atomic(paths.config, raw)
    created["config"] = paths.config
    # 8. LaunchAgent plist(s)
    label = _label(paths)
    pdir = plist_dir(args)
    os.makedirs(pdir, exist_ok=True)
    plists = [(label, templates.AGENT_PLIST, {})]
    if getattr(args, "dashboard", False):
        plists.append((label + ".dashboard", templates.DASHBOARD_PLIST, {"PORT": getattr(args, "dashboard_port", 8765)}))
    written = []
    for lab, tmpl, extra in plists:
        text = render_plist(tmpl, LABEL=lab, VENV_PYTHON=venv_py, HOME_DIR=home, **extra)
        target = os.path.join(pdir, lab + ".plist")
        if os.path.exists(target) and open(target).read() != text:
            backup = paths.p("rollback", f"{lab}.plist.{time.strftime('%Y%m%dT%H%M%S')}")
            shutil.copy2(target, backup)
            say(f"gsched install: existing {target} backed up to {backup}")
        tmp = target + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        written.append(target)
    created["plists"] = written
    # 9. load
    if not args.no_load:
        uid = os.getuid()
        for (lab, _, _), target in zip(plists, written):
            _run(["launchctl", "bootout", f"gui/{uid}/{lab}"])
            r = _run(["launchctl", "bootstrap", f"gui/{uid}", target])
            if r.returncode != 0:
                say(f"gsched install: launchctl bootstrap failed for {lab}: {r.stderr.strip()[:300]}")
                return 78
        if _wait_for_ping(10):
            from gsched.cli import rpc
            try:
                res, _ = rpc("status", {"brief": True}, timeout=5)
                from gsched.status import brief_line
                say(brief_line(res))
            except Exception:
                pass
        else:
            say(f"gsched install: the daemon did not answer within 10 s; see {paths.p('logs', 'launchd.err.log')}")
    # 10. next steps
    say(f"\nInstalled gsched {__version__} into {home}.\nNext steps:")
    say(f'  1. Add to PATH:  export PATH="{paths.bin}:$PATH"   (or ln -s {wrapper} ~/.local/bin/gsched)')
    say("  2. Run `gsched doctor`. Folders under ~/Documents, ~/Desktop and ~/Downloads need Full Disk Access for\n"
        f"     {os.path.realpath(venv_py)} (System Settings > Privacy & Security > Full Disk Access).")
    say('  3. Optional MCP: claude mcp add gpu-scheduler -- gsched mcp   (see README.md)')
    say("  4. When you are ready to schedule for real: gsched config set enforce true")
    print(json.dumps({"home": home, "version": __version__, "loaded": not args.no_load, **created}))
    return 0


def _wait_for_ping(seconds: float) -> bool:
    from gsched import client
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if client.call("ping", {}, timeout=2).get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def uninstall(args) -> int:
    paths = Paths.resolve()
    label = _label(paths)
    pdir = plist_dir(args)
    # refuse while jobs run (unless --keep-data or --force)
    running = 0
    try:
        from gsched.cli import rpc
        res, _ = rpc("status", {"brief": True}, timeout=3)
        running = len(res.get("running", [])) if res else 0
    except Exception:
        running = 0
    if running and not args.keep_data and not args.force:
        say(f"gsched uninstall: {running} job(s) are running; drain first or pass --force")
        return 2
    uid = os.getuid()
    for lab in (label, label + ".dashboard"):
        target = os.path.join(pdir, lab + ".plist")
        if not args.no_load and os.path.exists(target):
            _run(["launchctl", "bootout", f"gui/{uid}/{lab}"])
        if os.path.exists(target):
            os.unlink(target)
            say(f"removed {target}")
    if not args.keep_data:
        for d in (paths.venv, paths.app, paths.bin):
            shutil.rmtree(d, ignore_errors=True)
        say(f"removed {paths.venv}, {paths.app}, {paths.bin}")
    if args.purge:
        for d in (paths.state, paths.logs, paths.jobs, paths.checkpoints):
            say(f"purging {d} ({_dir_size(d) / 2**20:.1f} MiB)")
            shutil.rmtree(d, ignore_errors=True)
    print(json.dumps({"uninstalled": True, "kept_data": not args.purge}))
    return 0
