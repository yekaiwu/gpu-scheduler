"""Submit-time checks that need the filesystem (03 section 5, steps 4, 6, 7). Called by the submit handler."""

from __future__ import annotations

import os
import shutil
import sys

from gsched.errors import GschedError


def tcc_hint(root: str | None = None) -> str:
    from gsched.tcc import hint_text
    return hint_text(root)


def _tcc_gate(tcc, path: str, what: str, field: str | None = None):
    """For a path the daemon is about to look at: (resolved, root). If the path lies in a TCC-protected root this
    never touches it. Raises cwd_not_accessible when the root is blocked, denied, or not probed yet."""
    resolved, root = tcc.resolve(path)
    if root is None:
        return resolved, None
    st = tcc.state(root)
    if st == "ok" or st == "absent":
        return resolved, root
    if st == "unknown":
        started = tcc.ensure_probe(root)
        raise GschedError("cwd_not_accessible", f"cannot check {what} {path} yet: access to {root} is still being probed",
                          path=path, root=root, reason="tcc_unknown_probe_started", tcc_unknown_probe_started=True,
                          hint="retry in a few seconds (a probe subprocess is checking whether the daemon may read "
                               f"{root}; it never blocks the daemon). " + tcc_hint(root), probe_started=started)
    reason = "tcc_blocked" if st == "blocked" else "tcc_denied"
    raise GschedError("cwd_not_accessible", f"the daemon cannot read {what} {path}: {root} is {st} by macOS (TCC)",
                      path=path, root=root, reason=reason, hint=tcc_hint(root))


def check_cwd(cwd: str, tcc=None, *, external: bool = False) -> str:
    """Return the resolved cwd if the daemon may use it. Inside a TCC-protected root nothing is touched: the cached
    probe state decides (`ok` -> assumed accessible; the workload's own errors show in its logs)."""
    if tcc is not None:
        resolved, root = tcc.resolve(cwd)
        if root is not None:
            if external:                 # the daemon never uses an external job's cwd: record it, touch nothing
                return resolved
            resolved, root = _tcc_gate(tcc, cwd, "cwd")
            if tcc.state(root) == "ok":
                return resolved
    try:
        os.listdir(cwd)
    except PermissionError:
        raise GschedError("cwd_not_accessible", f"cannot read cwd {cwd}", cwd=cwd, hint=tcc_hint())
    except (FileNotFoundError, NotADirectoryError):
        raise GschedError("invalid", f"cwd does not exist or is not a directory: {cwd}", field="cwd")
    except OSError as e:
        raise GschedError("cwd_not_accessible", f"cannot read cwd {cwd}: {e}", cwd=cwd, hint=tcc_hint())
    return os.path.realpath(cwd)


def check_log_path(field: str, path: str, tcc) -> None:
    """stdout/stderr paths are opened by the daemon itself (spawn_shim), so they must not be in a protected root."""
    root = tcc.root_for(path)
    if root is not None:
        raise GschedError("invalid", f"{field}: the log path must not be in a TCC-protected root ({root}); the daemon "
                          "opens it and macOS would block it behind a consent prompt. Use a path elsewhere "
                          "(the default is the job directory)", field=field, root=root)


def _which(c0: str, path_env: str, tcc) -> str | None:
    """shutil.which without touching PATH entries inside TCC-protected roots. Returns the path, None if not found.
    Returns c0 itself when the only possible homes were protected directories assumed accessible."""
    skipped_ok = False
    blocked = None
    for entry in path_env.split(os.pathsep):
        d_ = entry or "."
        if tcc is not None:
            resolved, root = tcc.resolve(d_)
            if root is not None:
                st = tcc.state(root)
                if st == "ok":
                    skipped_ok = True
                elif st != "absent":
                    if st == "unknown":
                        tcc.ensure_probe(root)
                    blocked = blocked or (d_, root)
                if st != "absent":
                    continue
        cand = os.path.join(d_, c0)
        try:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        except OSError:
            continue
    if skipped_ok or blocked:
        if blocked and not skipped_ok:
            _tcc_gate(tcc, blocked[0], "PATH entry")
        return c0
    return None


def resolve_command(command: tuple[str, ...], path_env: str, cwd: str | None = None, tcc=None) -> str:
    """Resolve command[0] the way the spawn will. Returns the resolved path (the argv is stored unchanged)."""
    c0 = command[0]
    if "/" in c0:
        p = c0 if os.path.isabs(c0) else os.path.join(cwd or "", c0)
        if tcc is not None:
            resolved, root = _tcc_gate(tcc, p, "command") if tcc.root_for(p) else (p, None)
            if root is not None and tcc.state(root) == "ok":
                return os.path.normpath(p)       # assumed present; the workload's exec error shows in its logs
        # os.path.isfile() swallows EPERM, which is exactly what TCC returns for ~/Documents etc. under launchd:
        # stat explicitly so the user gets the Full Disk Access hint instead of "command_not_found".
        import stat as _stat
        try:
            ok = _stat.S_ISREG(os.stat(p).st_mode) and os.access(p, os.X_OK)
        except PermissionError:
            raise GschedError("cwd_not_accessible", f"cannot stat {c0}", command=c0, hint=tcc_hint())
        except OSError:
            ok = False
        if not ok:
            raise GschedError("command_not_found", f"{c0} does not exist or is not executable", command=c0)
        return os.path.normpath(p)
    found = _which(c0, path_env, tcc) if tcc is not None else shutil.which(c0, path=path_env)
    if not found:
        raise GschedError("command_not_found", f"{c0!r} not found on the job PATH", command=c0, path=path_env)
    return found


def check_permissions(project, door: str, resolved_command0: str | None) -> None:
    if door == "mcp" and not project.mcp_submit:
        raise GschedError("forbidden", f"MCP submission is not enabled for project {project.name!r}; "
                          'set "mcp_submit": true in its project file', project=project.name)
    if project.command_allow is not None and resolved_command0 is not None:
        if not any(resolved_command0.startswith(p) for p in project.command_allow):
            raise GschedError("forbidden", "command is not allowed by the project's command_allow prefixes",
                              project=project.name)
