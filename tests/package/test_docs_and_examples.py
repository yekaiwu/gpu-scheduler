import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from gsched.config import Config
from gsched.errors import ConfigError
from gsched.projects import ProjectRegistry, load_profile, parse_project
from gsched.spec import parse_spec

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "local", "node_modules", "dist", "build"}


def test_readme_names_modes_queues_and_platform():
    txt = (ROOT / "README.md").read_text()
    for needle in ("restartable", "cooperative", "atomic", "night", "Apple silicon", "Full Disk Access", "gsched mcp"):
        assert needle in txt, needle
    for needle in ("gsched dashboard", "AGENTS.md", "enforce", "LICENSE"):
        assert needle in txt


def test_example_specs_validate():
    files = sorted((ROOT / "examples" / "jobs").glob("*.json"))
    assert {f.name for f in files} >= {"atomic.json", "cooperative.json", "restartable.json", "cpu-only.json"}
    for f in files:
        spec = parse_spec(json.loads(f.read_text()), Config.defaults(), door="cli")
        assert spec.project == "example-train"
    coop = parse_spec(json.loads((ROOT / "examples/jobs/cooperative.json").read_text()), Config.defaults(), door="cli")
    assert coop.estimate_s == 86400 and coop.queue_requested == "auto"


def test_example_projects_profiles_and_configs():
    for f in (ROOT / "examples" / "projects").glob("*.json"):
        parse_project(f.stem, json.loads(f.read_text()))
    for f in (ROOT / "examples" / "configs").glob("*.json") if (ROOT / "examples" / "configs").exists() else []:
        pass
    for f in (ROOT / "examples" / "config").glob("*.json"):
        Config.from_dict(json.loads(f.read_text()))
    assert Config.from_dict(json.loads((ROOT / "examples/config/dedicated.json").read_text())).night_window is None
    reg = ProjectRegistry(str(ROOT / "examples" / "projects"))
    reg.reload_if_changed()
    for f in (ROOT / "examples" / "profiles").glob("*.json"):
        raw = json.loads(f.read_text())
        with pytest.raises(ValueError):                                  # ${LLM_ROOT} unset: fails closed
            load_profile(f.stem, raw, Config.defaults(), reg, {})
        # the example profile's name carries a suffix; the loader wants name == stem
        raw["name"] = f.stem
        p = load_profile(f.stem, raw, Config.defaults(), reg, {"LLM_ROOT": "/opt/models"})
        assert p.spec.checkpoint == "restartable" and "port:8081" in p.spec.locks and p.spec.queue_requested == "day"


def test_examples_are_executable_where_expected_and_counter_runs():
    out = subprocess.run(["python3", str(ROOT / "examples/checkpoint_counter.py"), "--steps", "2", "--step-s", "0.01"],
                         capture_output=True, text=True, env={**os.environ, "GSCHED_CHECKPOINT_DIR": "/tmp/gs-counter-test"})
    assert out.returncode == 0 and out.stdout.strip().endswith("done")
    assert not os.path.exists("/tmp/gs-counter-test/READY")               # a finished run never writes READY
    import shutil
    shutil.rmtree("/tmp/gs-counter-test", ignore_errors=True)


def test_license_is_apache_2():
    lic = (ROOT / "LICENSE").read_text()
    assert "Apache License" in lic and "Version 2.0, January 2004" in lic and "END OF TERMS AND CONDITIONS" in lic


def test_agents_md_matches_the_documented_block():
    txt = (ROOT / "AGENTS.md").read_text()
    for needle in ("gsched run", "gsched wait", "--checkpoint atomic", "Never kill processes you did not start", "submit_job"):
        assert needle in txt


def test_git_check_ignore_equivalent():
    assert "docs/local/" in (ROOT / ".gitignore").read_text().splitlines()


def _tracked_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in {".pyc", ".sqlite", ".png", ".ico"}:
                continue
            yield p


def test_tree_has_no_personal_data():
    users = re.compile("/" + "Users/(?!(?:me|you|USER)\\b)[A-Za-z0-9._-]+")
    cgnat = re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b")
    keyfile = re.compile("api" + "-keys")
    privkey = re.compile("-----BEGIN [A-Z ]*PRIVATE" + " KEY-----")
    extra = []
    private = ROOT / "docs" / "local" / "private-patterns.txt"
    if private.exists():
        extra = [re.compile(l.strip()) for l in private.read_text().splitlines() if l.strip() and not l.startswith("#")]
    bad = []
    for p in _tracked_files():
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = p.relative_to(ROOT)
        for n, line in enumerate(text.splitlines(), 1):
            # the build doc that describes this very test names the patterns it scans for
            if str(rel).startswith("docs/implementation/") and "test_tree_has_no_personal_data" in line:
                continue
            for rx in (users, cgnat, keyfile, privkey, *extra):
                if rx.search(line):
                    bad.append(f"{rel}:{n}: {rx.pattern[:30]}")
    assert not bad, bad


def test_no_runtime_dependencies_and_stdlib_only():
    import ast
    import sys
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for p in (ROOT / "gsched").rglob("*.py"):
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                names = [n.module.split(".")[0]]
            for name in names:
                if name not in stdlib and name != "gsched":
                    offenders.append(f"{p.name}: {name}")
    assert not offenders, offenders
    assert 'dependencies = []' in (ROOT / "pyproject.toml").read_text()


def test_no_threads_in_daemon_modules():
    daemon_side = ["daemon.py", "tick.py", "supervise.py", "residents.py", "rpc.py", "store.py", "scan.py"]
    for name in daemon_side:
        txt = (ROOT / "gsched" / name).read_text()
        assert not re.search(r"^\s*(import|from)\s+(threading|asyncio|concurrent)", txt, re.M), name


def test_policy_is_pure():
    import ast
    banned = {"os", "time", "sqlite3", "socket", "subprocess", "random", "datetime_now"}
    for p in (ROOT / "gsched" / "policy").glob("*.py"):
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                assert not ({a.name.split(".")[0] for a in n.names} & {"sqlite3", "socket", "subprocess", "random", "time"}), p.name
            if isinstance(n, ast.ImportFrom) and n.module:
                assert n.module.split(".")[0] not in {"sqlite3", "socket", "subprocess", "random", "time"}, p.name
                assert not n.module.startswith(("gsched.store", "gsched.platform.darwin", "gsched.daemon", "gsched.client")), p.name
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                assert not (isinstance(n.func.value, ast.Name) and n.func.value.id in ("time", "os")), (p.name, n.lineno)


def test_no_machine_specific_constants():
    """No memsize, page size, core count, port or home path literal in gsched/ (documented config defaults aside)."""
    pat = re.compile(r"51539607552|\b16384\b|\b8081\b|\bhw\.physicalcpu\b\s*==|/private/|\.local/gpu-scheduler")
    allowed = {"fake.py": "the fake models a machine", "paths.py": "DEFAULT_HOME", "dashboard.py": "docs", "mcp_stdio.py": "docs",
               "rpc.py": "log tail cap", "darwin.py": "sysctl names"}
    for p in (ROOT / "gsched").rglob("*.py"):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if pat.search(line) and p.name not in allowed and not line.lstrip().startswith(("#", '"""')):
                raise AssertionError(f"{p.name}:{n}: {line.strip()}")
