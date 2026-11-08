"""macOS TCC guard for the single-threaded daemon (urgent addendum to PR 12).

A launchd-started process that opens or chdir()s into ~/Documents, ~/Desktop, ~/Downloads (or iCloud Drive) without
Full Disk Access does not get EPERM: the call BLOCKS, waiting for a consent prompt nobody will answer. In the daemon
that freezes everything. So the daemon process never touches a path inside a protected root. Instead:

* `resolve()` classifies a path lexically (following symlinks only OUTSIDE the roots, with readlink, which never
  enters a root) and tells which protected root, if any, contains it;
* a per-root access state (`ok` / `blocked` / `denied` / `absent`) is filled by a probe SUBPROCESS
  (`python -I -c "os.listdir(root)"`) that is polled through the selector and SIGKILLed after 3 s;
* callers (checks.py, doctor) consult the cached state and never touch the path themselves.

The probe runs at startup, every 600 s, and on demand when a submit hits a root whose state is still unknown.
"""

from __future__ import annotations

import os
import selectors
import sys
from dataclasses import dataclass

PROBE_SNIPPET = "import os,sys; os.listdir(sys.argv[1]); print('ok')"
PROBE_TIMEOUT_S = 3.0
REPROBE_S = 600.0
DEFAULT_ROOTS = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")
OK, BLOCKED, DENIED, ABSENT, UNKNOWN = "ok", "blocked", "denied", "absent", "unknown"


def default_home() -> str:
    return os.path.expanduser("~")


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _norm_roots(home: str, extra) -> list[str]:
    """Absolute roots, lexical and (where it is safe to compute) real. Keys of the access table are the lexical
    form; `real` forms only widen what counts as inside."""
    out: list[str] = []
    for rel in DEFAULT_ROOTS:
        out.append(os.path.normpath(os.path.join(home, rel)))
    for x in extra or ():
        out.append(os.path.normpath(os.path.expanduser(str(x))))
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def hint_text(root: str | None = None) -> str:
    exe = os.path.realpath(sys.executable)
    where = f" ({root})" if root else ""
    return ("macOS asks for consent before a launchd-started process may read ~/Documents, ~/Desktop, ~/Downloads "
            f"or iCloud Drive{where}, and nobody is there to click the prompt. Grant Full Disk Access to the daemon's "
            f"Python: {exe} (System Settings -> Privacy & Security -> Full Disk Access), then retry. "
            "Or keep job code outside those folders. `gsched doctor` shows the current state.")


@dataclass
class Entry:
    state: str
    t_mono: float
    detail: str = ""


@dataclass
class _Pending:
    root: str
    proc: object
    started_mono: float


class TccGuard:
    def __init__(self, daemon, home: str | None = None):
        self.d = daemon
        self.home = os.path.normpath(home if home is not None else default_home())
        self.entries: dict[str, Entry] = {}
        self.pending: dict[str, _Pending] = {}
        self._cache_key = None
        self._lex: list[str] = []
        self._real: dict[str, str] = {}

    # ---- roots / classification (no filesystem access inside a root, ever) -----------------------------------------
    def roots(self) -> list[str]:
        extra = tuple(getattr(self.d.cfg, "tcc_protected_roots", ()) or ())
        key = (self.home, extra)
        if key != self._cache_key:
            self._lex = _norm_roots(self.home, extra)
            # Real forms: realpath only touches the root's ancestors (`~` and its parents), never its contents.
            self._real = {}
            for r in self._lex:
                try:
                    rp = self._safe_real(r)
                except OSError:
                    rp = r
                self._real[r] = rp
            self._cache_key = key
        return list(self._lex)

    def _safe_real(self, path: str) -> str:
        """realpath for a root itself. Resolves symlinks of its ancestors; if an ancestor is itself inside another
        root the lexical path is used (no access)."""
        resolved, _root = self._resolve(path, self._lex, enter=False)
        return resolved

    def _root_of(self, path: str, lex: list[str]) -> str | None:
        for r in lex:
            if _within(path, r) or _within(path, self._real.get(r, r)):
                return r
        return None

    def _resolve(self, path: str, lex: list[str], enter: bool = True) -> tuple[str, str | None]:
        path = os.path.normpath(path if os.path.isabs(path) else os.path.abspath(path))
        todo = [c for c in path.split("/") if c]
        cur = "/"
        links = 0
        while todo:
            comp = todo.pop(0)
            if comp == ".":
                continue
            if comp == "..":
                cur = os.path.dirname(cur) or "/"
                continue
            nxt = os.path.join(cur, comp)
            if enter:
                root = self._root_of(nxt, lex)
                if root is not None:
                    rest = "/".join(todo)
                    return os.path.normpath(os.path.join(nxt, rest)) if rest else nxt, root
            try:
                target = os.readlink(nxt)
            except OSError:
                cur = nxt                 # not a symlink (or missing): keep walking
                continue
            links += 1
            if links > 40:
                return nxt, None
            if os.path.isabs(target):
                cur = "/"
            todo = [c for c in target.split("/") if c] + todo
        return cur, (self._root_of(cur, lex) if enter else None)

    def resolve(self, path: str) -> tuple[str, str | None]:
        """(resolved path, protected root or None). If a root is returned, `resolved` is lexical beyond it and the
        caller must NOT touch it from this process."""
        lex = self.roots()
        return self._resolve(path, lex)

    def root_for(self, path: str) -> str | None:
        return self.resolve(path)[1]

    # ---- state ----------------------------------------------------------------------------------------------------------
    def state(self, root: str) -> str:
        e = self.entries.get(root)
        return e.state if e is not None else UNKNOWN

    def ensure_probe(self, root: str) -> bool:
        """Start a probe for `root` unless one is running. Never blocks."""
        if root in self.pending:
            return False
        d = self.d
        try:
            proc = d.platform.spawn_async([sys.executable, "-I", "-c", PROBE_SNIPPET, root])
        except OSError as e:
            self.entries[root] = Entry(BLOCKED, d.clock.mono(), f"probe could not start: {e}")
            d.log.decision("tcc_probe", root=root, state=BLOCKED, detail=f"probe could not start: {e}")
            return False
        self.pending[root] = _Pending(root, proc, d.clock.mono())
        try:
            d.sel.register(proc.fileno(), selectors.EVENT_READ, d.make_key("tcc", root))
        except (ValueError, OSError, KeyError):
            pass
        self._poll_one(root)             # a fast (or fake) probe may already be finished
        return True

    def start_all(self) -> None:
        for r in self.roots():
            self.ensure_probe(r)

    def on_event(self, root: str) -> None:
        self._poll_one(root)

    def _poll_one(self, root: str) -> None:
        p = self.pending.get(root)
        if p is None:
            return
        res = p.proc.poll()
        if res is None:
            return
        rc, out = res
        self._finish(root, rc, out)

    def _finish(self, root: str, rc, out: str, forced: tuple | None = None) -> None:
        d = self.d
        p = self.pending.pop(root, None)
        if p is not None:
            try:
                d.sel.unregister(p.proc.fileno())
            except (KeyError, ValueError, OSError, TypeError):
                pass
            p.proc.close()
        if forced is not None:
            state, detail = forced
        elif rc == 0 and "ok" in out:
            state, detail = OK, ""
        elif "PermissionError" in out or "Operation not permitted" in out:
            state, detail = DENIED, "listing the root failed with EPERM"
        elif "FileNotFoundError" in out or "NotADirectoryError" in out:
            state, detail = ABSENT, "the root does not exist"
        else:
            state, detail = DENIED, (out.strip().splitlines() or ["probe failed"])[-1][:200]
        old = self.entries.get(root)
        self.entries[root] = Entry(state, d.clock.mono(), detail)
        if old is None or old.state != state:
            d.log.decision("tcc_probe", root=root, state=state, detail=detail)

    def tick(self) -> None:
        """Every full tick: finish probes, kill the ones past 3 s, schedule the 600 s re-probes."""
        d = self.d
        now = d.clock.mono()
        for root in list(self.pending):
            self._poll_one(root)
            p = self.pending.get(root)
            if p is not None and now - p.started_mono >= PROBE_TIMEOUT_S:
                d.platform.kill_async(p.proc)    # our own helper process, stuck behind a TCC prompt
                self._finish(root, -9, "", forced=(BLOCKED, f"no answer within {PROBE_TIMEOUT_S:g} s (TCC consent "
                                                   "pending, or the volume is hung); the probe was killed"))
        for root in self.roots():
            e = self.entries.get(root)
            if root not in self.pending and (e is None or now - e.t_mono >= REPROBE_S):
                self.ensure_probe(root)

    def shutdown(self) -> None:
        for root in list(self.pending):
            p = self.pending.pop(root)
            try:
                self.d.sel.unregister(p.proc.fileno())
            except (KeyError, ValueError, OSError, TypeError):
                pass
            self.d.platform.kill_async(p.proc)
            p.proc.close()

    # ---- reporting ----------------------------------------------------------------------------------------------------------
    def report(self) -> list[dict]:
        out = []
        for r in self.roots():
            e = self.entries.get(r)
            out.append({"root": r, "state": e.state if e else UNKNOWN, "detail": e.detail if e else "",
                        "pending": r in self.pending})
        return out
