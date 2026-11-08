"""SQLite schema, migrations, and the ONLY functions allowed to change job state (04).

Every state-changing function runs in one `BEGIN IMMEDIATE ... COMMIT` and returns only after the
commit. Callers write to sockets, pipes and signals only after it returns (ground rule 3).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from typing import Any, Iterable

from gsched import ids
from gsched.clock import Clock
from gsched.errors import StoreError
from gsched.paths import Paths, mkdir_private
from gsched.platform import JobIdentity

USER_VERSION = 2

SCHEMA = """
CREATE TABLE project (
  id TEXT PRIMARY KEY,
  weight INTEGER NOT NULL CHECK (weight >= 1),
  usage REAL NOT NULL DEFAULT 0,
  usage_t_ms INTEGER NOT NULL DEFAULT 0,
  present INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE job (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL REFERENCES project(id),
  name TEXT,
  profile TEXT,
  state TEXT NOT NULL CHECK (state IN ('needs_estimate','queued','held','done','failed','cancelled','lost')),
  held_reason TEXT CHECK (held_reason IN ('spawning','await_attach','live','checkpointing','spilled','stopping')),
  stop_reason TEXT,
  end_reason TEXT,
  queue TEXT CHECK (queue IN ('day','night')),
  queue_requested TEXT NOT NULL,
  priority INTEGER NOT NULL,
  checkpoint TEXT NOT NULL,
  spawn TEXT NOT NULL,
  gpu TEXT NOT NULL,
  memory_bytes INTEGER NOT NULL,
  cpu_cores INTEGER NOT NULL,
  locks_json TEXT NOT NULL DEFAULT '[]',
  ceiling_bytes INTEGER,
  level_min INTEGER,
  estimate_s INTEGER,
  limit_s INTEGER,
  grace_s INTEGER NOT NULL,
  on_timeout TEXT NOT NULL,
  allow_spill INTEGER NOT NULL,
  after_json TEXT NOT NULL DEFAULT '[]',
  command_json TEXT,
  cwd TEXT,
  env_json TEXT NOT NULL DEFAULT '{}',
  stdout_path TEXT,
  stderr_path TEXT,
  checkpoint_dir TEXT NOT NULL,
  comment TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT,
  door TEXT NOT NULL,
  client_pid INTEGER,
  attempt INTEGER NOT NULL DEFAULT 1,
  resume INTEGER NOT NULL DEFAULT 0,
  enqueued_ms INTEGER NOT NULL,
  submitted_ms INTEGER NOT NULL,
  eligible_since_ms INTEGER,
  held_ms INTEGER,
  started_ms INTEGER,
  stop_requested_ms INTEGER,
  kill_sent INTEGER NOT NULL DEFAULT 0,
  released_ms INTEGER,
  checkpoint_failed INTEGER NOT NULL DEFAULT 0,
  spilled INTEGER NOT NULL DEFAULT 0,
  spill_reason TEXT,
  over_limit INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  yield_requested INTEGER NOT NULL DEFAULT 0,
  estimate_overrun INTEGER NOT NULL DEFAULT 0,
  shim_pid INTEGER,
  shim_start_sec INTEGER,
  shim_start_usec INTEGER,
  pid INTEGER,
  pid_path TEXT,
  pid_start_sec INTEGER,
  pid_start_usec INTEGER,
  pgid INTEGER,
  gate_released INTEGER NOT NULL DEFAULT 0,
  lease_hash TEXT,
  hold INTEGER NOT NULL DEFAULT 0,
  ready INTEGER NOT NULL DEFAULT 0,
  last_touch_ms INTEGER,
  switch_to TEXT,
  listen_port INTEGER,
  exit_code INTEGER,
  exit_signal INTEGER,
  peak_footprint_bytes INTEGER,
  footprint_bytes INTEGER,
  footprint_fail_n INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX job_state ON job (state, queue, priority);
CREATE UNIQUE INDEX job_idempo ON job (project, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND state IN ('needs_estimate','queued','held');
CREATE UNIQUE INDEX job_profile_live ON job (profile)
  WHERE profile IS NOT NULL AND state IN ('queued','held');
CREATE TABLE offender (
  id INTEGER PRIMARY KEY, pid INTEGER, path TEXT, port INTEGER, class TEXT NOT NULL,
  footprint_bytes INTEGER, seen_ms INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL, cleared_ms INTEGER
);
CREATE TABLE event (
  id INTEGER PRIMARY KEY, t_ms INTEGER NOT NULL, kind TEXT NOT NULL, job TEXT, body_json TEXT NOT NULL
);
CREATE INDEX event_t ON event (t_ms);
CREATE INDEX event_job ON event (job, t_ms);
CREATE TABLE history (
  id INTEGER PRIMARY KEY, job TEXT NOT NULL, attempt INTEGER NOT NULL, project TEXT NOT NULL, name TEXT,
  checkpoint TEXT NOT NULL, gpu TEXT NOT NULL, memory_bytes INTEGER NOT NULL,
  peak_footprint_bytes INTEGER, runtime_s INTEGER, outcome TEXT NOT NULL, t_ms INTEGER NOT NULL
);
CREATE INDEX history_key ON history (project, name, t_ms);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

TERMINAL = ("done", "failed", "cancelled", "lost")
LIVE_STATES = ("needs_estimate", "queued", "held")
FLAG_COLUMNS = {"over_limit", "yield_requested", "estimate_overrun", "ready", "footprint_bytes", "footprint_fail_n",
                "peak_footprint_bytes", "last_touch_ms", "hold", "switch_to", "kill_sent", "eligible_since_ms"}


def finish_outcome(row, exit_code: int | None, signal: int | None) -> tuple[str, str]:
    """Map an exit record to (state, end_reason) per 04 section 4."""
    if row["cancel_requested"]:
        return "cancelled", "cancelled"
    if signal is not None:
        return "failed", "signal"
    if exit_code == 0:
        return "done", "exit"
    return "failed", "exit"


class Store:
    def __init__(self, paths: Paths, clock: Clock, db_path: str | None = None):
        self.paths = paths
        self.clock = clock
        self.path = db_path or paths.db
        self._depth = 0
        old = os.umask(0o077)
        try:
            mkdir_private(os.path.dirname(self.path))
            self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=True)
        finally:
            os.umask(old)
        c = self.conn
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=1000")
        self._migrate()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, 0o600)
            except FileNotFoundError:
                pass

    def _migrate(self) -> None:
        uv = self.conn.execute("PRAGMA user_version").fetchone()[0]
        tables = self.conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if uv == 0 and tables == 0:
            self.conn.execute("BEGIN IMMEDIATE")
            for stmt in SCHEMA.split(";\n"):
                if stmt.strip():
                    self.conn.execute(stmt)
            self.conn.execute(f"PRAGMA user_version={USER_VERSION}")
            self.conn.execute("INSERT INTO meta(key,value) VALUES('schema_created_ms',?)",
                              (str(self.clock.now_ms()),))
            self.conn.execute("COMMIT")
        elif uv != USER_VERSION:
            raise StoreError(f"database schema version {uv} is not supported; move {self.path} aside "
                             "(do NOT do this while jobs are running)")

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ---- transactions -------------------------------------------------------------------
    @contextlib.contextmanager
    def _tx(self):
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    batch = _tx  # one transaction for many small updates (per-tick bookkeeping)

    def _update(self, sql: str, params: Iterable[Any]) -> bool:
        with self._tx():
            return self.conn.execute(sql, tuple(params)).rowcount == 1

    # ---- meta -------------------------------------------------------------------------------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def meta_set(self, key: str, value: str) -> None:
        with self._tx():
            self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, str(value)))

    def meta_delete(self, key: str) -> None:
        with self._tx():
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def meta_prefix(self, prefix: str) -> dict[str, str]:
        rows = self.conn.execute("SELECT key,value FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {r[0]: r[1] for r in rows}

    # ---- projects -----------------------------------------------------------------------------
    def upsert_project(self, name: str, weight: int, present: bool = True) -> None:
        with self._tx():
            self.conn.execute(
                "INSERT INTO project(id,weight,present) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET weight=excluded.weight, present=excluded.present",
                (name, weight, 1 if present else 0))

    def mark_project_absent(self, name: str) -> None:
        with self._tx():
            self.conn.execute("UPDATE project SET present=0 WHERE id=?", (name,))

    def projects(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM project ORDER BY id").fetchall()

    def set_project_usage(self, name: str, usage: float, t_ms: int) -> None:
        with self._tx():
            self.conn.execute("UPDATE project SET usage=?, usage_t_ms=? WHERE id=?", (usage, t_ms, name))

    # ---- insert -------------------------------------------------------------------------------------
    def insert_job(self, spec, *, queue: str | None, state: str, door: str, now_ms: int, client_pid: int | None = None,
                   profile: str | None = None, hold: bool = False, last_touch_ms: int | None = None,
                   listen_port: int | None = None, cwd: str | None = None) -> str:
        assert state in ("queued", "needs_estimate")
        for attempt in range(5):
            job_id = ids.new_job_id(now_ms)
            ckdir = self.paths.checkpoint_dir(job_id)
            vals = dict(
                id=job_id, project=spec.project, name=spec.name, profile=profile, state=state, queue=queue,
                queue_requested=spec.queue_requested, priority=spec.priority, checkpoint=spec.checkpoint,
                spawn=spec.spawn, gpu=spec.gpu, memory_bytes=spec.memory_bytes, cpu_cores=spec.cpu_cores,
                locks_json=json.dumps(list(spec.locks)), ceiling_bytes=spec.ceiling_bytes, level_min=spec.level_min,
                estimate_s=spec.estimate_s, limit_s=spec.limit_s, grace_s=spec.grace_s, on_timeout=spec.on_timeout,
                allow_spill=int(spec.allow_spill), after_json=json.dumps(list(spec.after)),
                command_json=json.dumps(list(spec.command)) if spec.command is not None else None,
                cwd=cwd if cwd is not None else spec.cwd, env_json=json.dumps(dict(spec.env)),
                stdout_path=spec.stdout_path, stderr_path=spec.stderr_path, checkpoint_dir=ckdir,
                comment=spec.comment, idempotency_key=spec.idempotency_key, door=door, client_pid=client_pid,
                enqueued_ms=now_ms, submitted_ms=now_ms, hold=int(hold), last_touch_ms=last_touch_ms,
                listen_port=listen_port)
            cols = ",".join(vals)
            qs = ",".join("?" for _ in vals)
            try:
                with self._tx():
                    self.conn.execute(f"INSERT INTO job({cols}) VALUES({qs})", tuple(vals.values()))
            except sqlite3.IntegrityError as e:
                if str(e).strip().endswith("job.id"):
                    continue
                raise
            # directories are created after the row exists; they are private and idempotent
            jd = self.paths.job_dir(job_id)
            mkdir_private(jd)
            mkdir_private(ckdir)
            doc = {"job": job_id, "project": spec.project, "name": spec.name, "checkpoint": spec.checkpoint,
                   "gpu": spec.gpu, "memory_bytes": spec.memory_bytes, "cpu_cores": spec.cpu_cores,
                   "command": list(spec.command) if spec.command else None, "cwd": vals["cwd"],
                   "env_keys": sorted(spec.env), "estimate_s": spec.estimate_s, "limit_s": spec.limit_s,
                   "priority": spec.priority, "queue": queue, "after": list(spec.after), "spawn": spec.spawn}
            fd = os.open(os.path.join(jd, "spec.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh, indent=1)
            return job_id
        raise StoreError("could not allocate a unique job id")

    # ---- reads ------------------------------------------------------------------------------------------
    def get(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()

    def live_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM job WHERE state IN ('needs_estimate','queued','held') ORDER BY enqueued_ms, id").fetchall()

    def held_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM job WHERE state='held' ORDER BY held_ms, id").fetchall()

    running_jobs = held_jobs

    def rows_by_ids(self, id_list: Iterable[str]) -> list[sqlite3.Row]:
        id_list = list(id_list)
        if not id_list:
            return []
        qs = ",".join("?" for _ in id_list)
        return self.conn.execute(f"SELECT * FROM job WHERE id IN ({qs})", id_list).fetchall()

    def states_of(self, id_list: Iterable[str]) -> dict[str, str]:
        return {r["id"]: r["state"] for r in self.rows_by_ids(id_list)}

    def list_jobs(self, *, states: list[str] | None = None, project: str | None = None, limit: int = 100,
                  since_ms: int | None = None) -> list[sqlite3.Row]:
        q, p = "SELECT * FROM job WHERE 1=1", []
        if states:
            q += f" AND state IN ({','.join('?' for _ in states)})"
            p += states
        if project:
            q += " AND project=?"
            p.append(project)
        if since_ms is not None:
            q += " AND (state NOT IN ('done','failed','cancelled','lost') OR released_ms >= ?)"
            p.append(since_ms)
        q += " ORDER BY submitted_ms DESC, id DESC LIMIT ?"
        p.append(limit)
        return self.conn.execute(q, p).fetchall()

    def find_idempotent(self, project: str, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM job WHERE project=? AND idempotency_key=? AND state IN ('needs_estimate','queued','held')",
            (project, key)).fetchone()

    def live_profile_row(self, profile: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM job WHERE profile=? AND state IN ('queued','held')",
                                 (profile,)).fetchone()

    def by_lease(self, token: str) -> sqlite3.Row | None:
        h = ids.lease_hash(token) if isinstance(token, str) else None
        if h is None:
            return None
        return self.conn.execute("SELECT * FROM job WHERE lease_hash=?", (h,)).fetchone()

    # ---- transitions (one owner each) ---------------------------------------------------------------------------
    def set_estimate(self, job_id: str, estimate_s: int, queue: str, now_ms: int) -> bool:
        return self._update(
            "UPDATE job SET estimate_s=?, queue=?, state='queued', enqueued_ms=CASE WHEN state='needs_estimate' THEN ? "
            "ELSE enqueued_ms END WHERE id=? AND state IN ('needs_estimate','queued')",
            (estimate_s, queue, now_ms, job_id))

    def commit_admit(self, job_id: str, now_ms: int, *, lease_hash: str | None = None) -> bool:
        """The admission authority. queued -> held/spawning (daemon) or held/await_attach (external)."""
        return self._update(
            "UPDATE job SET state='held', held_reason=CASE WHEN spawn='external' THEN 'await_attach' ELSE 'spawning' END, "
            "held_ms=?, eligible_since_ms=NULL, lease_hash=? WHERE id=? AND state='queued'",
            (now_ms, lease_hash, job_id))

    def record_shim(self, job_id: str, pid: int, start_sec: int, start_usec: int) -> bool:
        return self._update(
            "UPDATE job SET shim_pid=?, shim_start_sec=?, shim_start_usec=? "
            "WHERE id=? AND state='held' AND held_reason='spawning'", (pid, start_sec, start_usec, job_id))

    def begin_child(self, job_id: str, ident: JobIdentity, now_ms: int, *, ready: bool = True) -> bool:
        """Record the workload identity. Daemon spawn: the gate stays closed. External: held/live at once."""
        with self._tx():
            r = self.conn.execute(
                "SELECT spawn FROM job WHERE id=? AND state='held' AND held_reason IN ('spawning','await_attach') "
                "AND pid IS NULL", (job_id,)).fetchone()
            if r is None:
                return False
            common = (ident.pid, ident.path, ident.start_sec, ident.start_usec, ident.pgid)
            if r["spawn"] == "external":
                cur = self.conn.execute(
                    "UPDATE job SET pid=?, pid_path=?, pid_start_sec=?, pid_start_usec=?, pgid=?, held_reason='live', "
                    "gate_released=1, started_ms=?, ready=? WHERE id=?", common + (now_ms, int(ready), job_id))
            else:
                cur = self.conn.execute(
                    "UPDATE job SET pid=?, pid_path=?, pid_start_sec=?, pid_start_usec=?, pgid=? WHERE id=?",
                    common + (job_id,))
            return cur.rowcount == 1

    def mark_gate_released(self, job_id: str, now_ms: int, *, ready: bool = True) -> bool:
        return self._update(
            "UPDATE job SET held_reason='live', gate_released=1, started_ms=?, ready=? "
            "WHERE id=? AND state='held' AND held_reason='spawning' AND pid IS NOT NULL AND gate_released=0",
            (now_ms, int(ready), job_id))

    def refresh_pid_path(self, job_id: str, path: str) -> bool:
        """The one place the recorded path may change: after exec (07 section 2)."""
        return self._update("UPDATE job SET pid_path=? WHERE id=? AND state='held' AND pid IS NOT NULL",
                            (path, job_id))

    def clear_shim(self, job_id: str) -> bool:
        return self._update("UPDATE job SET shim_pid=NULL, shim_start_sec=NULL, shim_start_usec=NULL "
                            "WHERE id=? AND state='held'", (job_id,))

    def request_checkpoint(self, job_id: str, now_ms: int, reason: str) -> bool:
        return self._update(
            "UPDATE job SET held_reason='checkpointing', stop_requested_ms=?, stop_reason=? "
            "WHERE id=? AND state='held' AND held_reason='live' AND checkpoint='cooperative'",
            (now_ms, reason, job_id))

    def mark_spilled(self, job_id: str, reason: str) -> bool:
        return self._update(
            "UPDATE job SET held_reason='spilled', checkpoint_failed=1, spilled=1, spill_reason=? "
            "WHERE id=? AND state='held' AND held_reason='checkpointing'", (reason, job_id))

    def begin_stop(self, job_id: str, now_ms: int, reason: str) -> bool:
        return self._update(
            "UPDATE job SET held_reason='stopping', stop_reason=?, stop_requested_ms=?, kill_sent=0 "
            "WHERE id=? AND state='held' AND (held_reason='live' "
            "OR (held_reason='spilled' AND ?='terminate') "
            "OR (held_reason='checkpointing' AND ? IN ('terminate','checkpoint_timeout_kill')))",
            (reason, now_ms, job_id, reason, reason))

    def requeue_continuation(self, job_id: str, now_ms: int, queue: str | None) -> bool:
        return self._requeue(job_id, now_ms, "held_reason='checkpointing'", resume=1, bump=1, queue=queue)

    def requeue_unstarted(self, job_id: str, now_ms: int) -> bool:
        return self._requeue(job_id, now_ms,
                             "held_reason IN ('spawning','await_attach') AND gate_released=0", resume=None, bump=0,
                             queue=None, history=False)

    def requeue_restartable(self, job_id: str, now_ms: int, *, to_state: str = "queued",
                            exit_code: int | None = None, exit_signal: int | None = None,
                            end_reason: str | None = None) -> bool:
        if to_state == "queued":
            return self._requeue(job_id, now_ms, "held_reason='stopping'", resume=0, bump=1, queue=None)
        with self._tx():
            row = self.get(job_id)
            if row is None or row["state"] != "held" or row["held_reason"] != "stopping":
                return False
            self._history(row, "done", now_ms)
            return self._terminal_update(job_id, now_ms, "done", end_reason or "unload", exit_code, exit_signal,
                                         where="held_reason='stopping'")

    def _requeue(self, job_id: str, now_ms: int, cond: str, *, resume, bump: int, queue, history: bool = True) -> bool:
        with self._tx():
            row = self.conn.execute(f"SELECT * FROM job WHERE id=? AND state='held' AND {cond}", (job_id,)).fetchone()
            if row is None:
                return False
            if history:
                self._history(row, "requeue", now_ms)
            sets = ["state='queued'", "held_reason=NULL", "stop_reason=NULL", "stop_requested_ms=NULL", "kill_sent=0",
                    "shim_pid=NULL", "shim_start_sec=NULL", "shim_start_usec=NULL", "pid=NULL", "pid_path=NULL",
                    "pid_start_sec=NULL", "pid_start_usec=NULL", "pgid=NULL", "gate_released=0", "started_ms=NULL",
                    "held_ms=NULL", "ready=0", "footprint_bytes=NULL", "footprint_fail_n=0", "eligible_since_ms=NULL",
                    "over_limit=0", "estimate_overrun=0", "yield_requested=0", "spilled=0", "checkpoint_failed=0",
                    "spill_reason=NULL", "lease_hash=NULL", "peak_footprint_bytes=NULL", "switch_to=NULL"]
            params: list[Any] = []
            if bump:
                sets.append("attempt=attempt+1")
            if resume is not None:
                sets.append("resume=?")
                params.append(resume)
            if bump:
                sets.append("enqueued_ms=?")
                params.append(now_ms)
            if queue is not None:
                sets.append("queue=?")
                params.append(queue)
            params.append(job_id)
            return self.conn.execute(f"UPDATE job SET {','.join(sets)} WHERE id=?", params).rowcount == 1

    def finish(self, job_id: str, now_ms: int, *, exit_code: int | None = None, exit_signal: int | None = None,
               peak_footprint_bytes: int | None = None, state: str | None = None,
               end_reason: str | None = None) -> str | None:
        """held/* -> done|failed|cancelled. Returns the new state, or None if the row was not held."""
        with self._tx():
            row = self.get(job_id)
            if row is None or row["state"] != "held":
                return None
            if state is None:
                state, er = finish_outcome(row, exit_code, exit_signal)
                end_reason = end_reason or er
            if peak_footprint_bytes is not None:
                old = row["peak_footprint_bytes"] or 0
                self.conn.execute("UPDATE job SET peak_footprint_bytes=? WHERE id=?",
                                  (max(old, peak_footprint_bytes), job_id))
                row = self.get(job_id)
            self._history(row, state, now_ms)
            ok = self._terminal_update(job_id, now_ms, state, end_reason, exit_code, exit_signal)
            return state if ok else None

    def mark_lost(self, job_id: str, now_ms: int, end_reason: str) -> bool:
        with self._tx():
            row = self.get(job_id)
            if row is None or row["state"] != "held":
                return False
            self._history(row, "lost", now_ms)
            return self._terminal_update(job_id, now_ms, "lost", end_reason, None, None)

    def _terminal_update(self, job_id, now_ms, state, end_reason, exit_code, exit_signal, where: str = "1=1") -> bool:
        return self.conn.execute(
            f"UPDATE job SET state=?, end_reason=?, exit_code=?, exit_signal=?, released_ms=?, held_reason=NULL, "
            f"eligible_since_ms=NULL WHERE id=? AND state='held' AND {where}",
            (state, end_reason, exit_code, exit_signal, now_ms, job_id)).rowcount == 1

    def cancel_unstarted(self, job_id: str, now_ms: int, end_reason: str) -> bool:
        return self._update(
            "UPDATE job SET state='cancelled', end_reason=?, released_ms=?, eligible_since_ms=NULL "
            "WHERE id=? AND state IN ('needs_estimate','queued')", (end_reason, now_ms, job_id))

    def shift_stop_timers(self, skew_ms: int, now_ms: int) -> int:
        """Clock-skew correction (16 section 3.3): move `stop_requested_ms` of every stopping/checkpointing held job by
        `skew_ms` (forward after a sleep, backward after a backward step), never beyond `now`. Returns the row count."""
        with self._tx():
            return self.conn.execute(
                "UPDATE job SET stop_requested_ms=MIN(stop_requested_ms+?, ?) WHERE state='held' "
                "AND held_reason IN ('stopping','checkpointing') AND stop_requested_ms IS NOT NULL",
                (skew_ms, now_ms)).rowcount

    def mark_cancel_requested(self, job_id: str) -> bool:
        return self._update("UPDATE job SET cancel_requested=1 WHERE id=? AND state='held'", (job_id,))

    def set_flags(self, job_id: str, **kw) -> bool:
        """Non-state flag updates (whitelisted columns). Allowed on live rows."""
        bad = set(kw) - FLAG_COLUMNS
        if bad:
            raise ValueError(f"set_flags: not a flag column: {sorted(bad)}")
        if not kw:
            return True
        sets = ",".join(f"{k}=?" for k in kw)
        return self._update(f"UPDATE job SET {sets} WHERE id=? AND state IN ('needs_estimate','queued','held')",
                            list(kw.values()) + [job_id])

    # ---- history / events -------------------------------------------------------------------------------------------
    def _history(self, row, outcome: str, now_ms: int) -> None:
        runtime = None
        if row["started_ms"] is not None:
            runtime = max(0, int((now_ms - row["started_ms"]) / 1000))
        self.conn.execute(
            "INSERT INTO history(job,attempt,project,name,checkpoint,gpu,memory_bytes,peak_footprint_bytes,runtime_s,"
            "outcome,t_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["attempt"], row["project"], row["name"], row["checkpoint"], row["gpu"],
             row["memory_bytes"], row["peak_footprint_bytes"], runtime, outcome, now_ms))

    def history_rows(self, project: str, name: str | None, limit: int = 50) -> list[sqlite3.Row]:
        if name is None:
            return self.conn.execute("SELECT * FROM history WHERE project=? AND outcome IN ('done','requeue') "
                                     "ORDER BY t_ms DESC, id DESC LIMIT ?", (project, limit)).fetchall()
        return self.conn.execute("SELECT * FROM history WHERE project=? AND name=? AND outcome IN ('done','requeue') "
                                 "ORDER BY t_ms DESC, id DESC LIMIT ?", (project, name, limit)).fetchall()

    def add_event(self, t_ms: int, kind: str, job: str | None, body: dict) -> None:
        with self._tx():
            self.conn.execute("INSERT INTO event(t_ms,kind,job,body_json) VALUES(?,?,?,?)",
                              (t_ms, kind, job, json.dumps(body, default=str, separators=(",", ":"))))

    def events(self, *, kinds: list[str] | None = None, exclude_kinds: list[str] | None = None,
               since_ms: int = 0, job: str | None = None, limit: int = 2000, newest_first: bool = False) -> list[dict]:
        q, p = "SELECT t_ms,kind,job,body_json FROM event WHERE t_ms>=?", [since_ms]
        if kinds:
            q += f" AND kind IN ({','.join('?' for _ in kinds)})"
            p += kinds
        if exclude_kinds:
            q += f" AND kind NOT IN ({','.join('?' for _ in exclude_kinds)})"
            p += exclude_kinds
        if job:
            q += " AND job=?"
            p.append(job)
        # newest `limit` rows, returned oldest first unless newest_first
        q += " ORDER BY t_ms DESC, id DESC LIMIT ?"
        p.append(limit)
        rows = self.conn.execute(q, p).fetchall()
        out = [{"t_ms": r["t_ms"], "kind": r["kind"], "job": r["job"], "body": json.loads(r["body_json"])} for r in rows]
        if not newest_first:
            out.reverse()
        return out

    # ---- offenders ---------------------------------------------------------------------------------------------------------
    def offender_open(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM offender WHERE cleared_ms IS NULL ORDER BY id").fetchall()

    def offenders_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM offender ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def offender_insert(self, *, pid, path, port, cls, footprint_bytes, now_ms) -> int:
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO offender(pid,path,port,class,footprint_bytes,seen_ms,last_seen_ms) VALUES(?,?,?,?,?,?,?)",
                (pid, path, port, cls, footprint_bytes, now_ms, now_ms))
            return cur.lastrowid

    def offender_touch(self, oid: int, footprint_bytes, now_ms, pid=None) -> None:
        """`pid` (a port offender's owner, learned later from the non-blocking lsof lookup) is set only if still NULL."""
        with self._tx():
            self.conn.execute("UPDATE offender SET last_seen_ms=?, footprint_bytes=?, pid=COALESCE(pid, ?) WHERE id=?",
                              (now_ms, footprint_bytes, pid, oid))

    def offender_clear(self, oid: int, now_ms) -> None:
        with self._tx():
            self.conn.execute("UPDATE offender SET cleared_ms=? WHERE id=? AND cleared_ms IS NULL", (now_ms, oid))

    # ---- retention -----------------------------------------------------------------------------------------------------------
    def retention(self, now_ms: int, days: float) -> dict:
        cutoff = now_ms - int(days * 86400 * 1000)
        with self._tx():
            ev = self.conn.execute("DELETE FROM event WHERE t_ms<?", (cutoff,)).rowcount
            off = self.conn.execute("DELETE FROM offender WHERE cleared_ms IS NOT NULL AND cleared_ms<?",
                                    (cutoff,)).rowcount
            old = self.conn.execute(
                "SELECT id,state,end_reason,checkpoint_dir FROM job WHERE state IN ('done','failed','cancelled','lost') "
                "AND released_ms IS NOT NULL AND released_ms<?", (cutoff,)).fetchall()
        dirs = 0
        for r in old:
            jd = self.paths.job_dir(r["id"])
            if os.path.isdir(jd):
                shutil.rmtree(jd, ignore_errors=True)
                dirs += 1
            ck = r["checkpoint_dir"]
            keep = r["state"] == "cancelled" and ck and os.path.exists(os.path.join(ck, "READY"))
            if ck and not keep and os.path.isdir(ck):
                shutil.rmtree(ck, ignore_errors=True)
        return {"events": ev, "offenders": off, "job_dirs": dirs}

    # ---- invariants -------------------------------------------------------------------------------------------------------------
    def check_invariants(self, budget_bytes: int | None = None, gpu_shared_max: int | None = None) -> list[str]:
        bad: list[str] = []
        held = self.held_jobs()
        ex = [r for r in held if r["gpu"] == "exclusive"]
        sh = [r for r in held if r["gpu"] == "shared"]
        if len(ex) > 1:
            bad.append("more than one exclusive held job")
        if ex and sh:
            bad.append("exclusive and shared held together")
        if gpu_shared_max is not None and len(sh) > gpu_shared_max:
            bad.append("shared gpu max exceeded")
        if budget_bytes is not None and sum(r["memory_bytes"] for r in held) > budget_bytes:
            bad.append("memory budget exceeded")
        seen: dict[str, str] = {}
        for r in held:
            for l in json.loads(r["locks_json"]):
                if l in seen:
                    bad.append(f"lock {l} held twice")
                seen[l] = r["id"]
            if r["held_reason"] == "live" and r["pid"] is None:
                bad.append(f"{r['id']} live without pid")
        for r in self.conn.execute("SELECT * FROM job WHERE state NOT IN ('held','done','failed','cancelled','lost') "
                                   "AND pid IS NOT NULL").fetchall():
            bad.append(f"{r['id']} not held but has pid")
        for r in self.conn.execute("SELECT * FROM job WHERE lease_hash IS NOT NULL AND spawn!='external'").fetchall():
            bad.append(f"{r['id']} has lease but is not external")
        return bad
