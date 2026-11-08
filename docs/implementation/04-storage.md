# 04: Storage

`gsched/store.py`. SQLite via stdlib `sqlite3`. The daemon is the only writer. The CLI and MCP never open the database: they go through the socket.

## 1. Connection

```python
conn = sqlite3.connect(path, isolation_level=None, check_same_thread=True)  # autocommit; explicit BEGIN
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")     # WAL+NORMAL: durable across process crash; may lose the last txn on power loss
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=1000")
conn.row_factory = sqlite3.Row
```

> **Power-loss note.** With `synchronous=NORMAL` in WAL mode, a commit is durable against a daemon crash but not against sudden power loss. The guarantee we need ("committed before we reply") is about crash ordering, so NORMAL is enough. If a user wants power-loss durability: `"sqlite_synchronous": "FULL"` (an optional config key; add it if requested).

Every state-changing function in `store.py` follows one pattern:

```python
def _tx(self):              # context manager
    self.conn.execute("BEGIN IMMEDIATE")
    try: yield; self.conn.execute("COMMIT")
    except: self.conn.execute("ROLLBACK"); raise
```

The function returns only after `COMMIT`. Callers write to sockets and pipes only after it returns. That is ground rule 3.

File mode: create the file (and `-wal`, `-shm`) under `umask 077`. After opening, `os.chmod(path, 0o600)`.

## 2. Schema (`user_version = 2`)

```sql
CREATE TABLE project (
  id              TEXT PRIMARY KEY,
  weight          INTEGER NOT NULL CHECK (weight >= 1),
  usage           REAL    NOT NULL DEFAULT 0,      -- decayed resource-seconds
  usage_t_ms      INTEGER NOT NULL DEFAULT 0,      -- when usage was last decayed
  present         INTEGER NOT NULL DEFAULT 1       -- 0 if the file was removed; rows are never deleted while jobs reference them
);

CREATE TABLE job (
  id                 TEXT PRIMARY KEY,             -- 20261108T231500-7f3a
  project            TEXT NOT NULL REFERENCES project(id),
  name               TEXT,
  profile            TEXT,                         -- set when created by ensure
  state              TEXT NOT NULL CHECK (state IN ('needs_estimate','queued','held','done','failed','cancelled','lost')),
  held_reason        TEXT CHECK (held_reason IN ('spawning','await_attach','live','checkpointing','spilled','stopping')),
  stop_reason        TEXT,                         -- why 'stopping': preempt, morning, pressure, idle, protective, switch, cancel, terminate, limit, ceiling, abort, drain
  end_reason         TEXT,                         -- terminal detail: exit, signal, spawn_error, dependency_failed, identity_mismatch, exit_unknown, cancelled_before_start, waiter_gone, attach_timeout, ...
  queue              TEXT CHECK (queue IN ('day','night')),
  queue_requested    TEXT NOT NULL,
  priority           INTEGER NOT NULL,
  checkpoint         TEXT NOT NULL,
  spawn              TEXT NOT NULL,
  gpu                TEXT NOT NULL,
  memory_bytes       INTEGER NOT NULL,
  cpu_cores          INTEGER NOT NULL,
  locks_json         TEXT NOT NULL DEFAULT '[]',
  ceiling_bytes      INTEGER,
  level_min          INTEGER,
  estimate_s         INTEGER,
  limit_s            INTEGER,
  grace_s            INTEGER NOT NULL,
  on_timeout         TEXT NOT NULL,
  allow_spill        INTEGER NOT NULL,
  after_json         TEXT NOT NULL DEFAULT '[]',
  command_json       TEXT,
  cwd                TEXT,
  env_json           TEXT NOT NULL DEFAULT '{}',   -- never logged, never returned
  stdout_path        TEXT,
  stderr_path        TEXT,
  checkpoint_dir     TEXT NOT NULL,
  comment            TEXT NOT NULL DEFAULT '',
  idempotency_key    TEXT,
  door               TEXT NOT NULL,                -- cli, mcp, profile
  client_pid         INTEGER,
  -- lifecycle
  attempt            INTEGER NOT NULL DEFAULT 1,
  resume             INTEGER NOT NULL DEFAULT 0,
  enqueued_ms        INTEGER NOT NULL,             -- reset on cooperative requeue
  submitted_ms       INTEGER NOT NULL,             -- never reset
  eligible_since_ms  INTEGER,                      -- first tick it was eligible but didn't fit; null when it fits or isn't eligible
  held_ms            INTEGER,
  started_ms         INTEGER,                      -- workload exec released; limit clock
  stop_requested_ms  INTEGER,                      -- SIGTERM sent (for kill_grace_s) or checkpoint requested (for grace_s)
  kill_sent          INTEGER NOT NULL DEFAULT 0,   -- SIGKILL escalation already sent
  released_ms        INTEGER,
  -- flags
  checkpoint_failed  INTEGER NOT NULL DEFAULT 0,
  spilled            INTEGER NOT NULL DEFAULT 0,
  spill_reason       TEXT,
  over_limit         INTEGER NOT NULL DEFAULT 0,
  cancel_requested   INTEGER NOT NULL DEFAULT 0,
  yield_requested    INTEGER NOT NULL DEFAULT 0,
  estimate_overrun   INTEGER NOT NULL DEFAULT 0,
  -- process identity (workload) and shim
  shim_pid           INTEGER,
  shim_start_sec     INTEGER,
  shim_start_usec    INTEGER,
  pid                INTEGER,
  pid_path           TEXT,
  pid_start_sec      INTEGER,
  pid_start_usec     INTEGER,
  pgid               INTEGER,
  gate_released      INTEGER NOT NULL DEFAULT 0,
  lease_hash         TEXT,
  -- resident
  hold               INTEGER NOT NULL DEFAULT 0,
  ready              INTEGER NOT NULL DEFAULT 0,
  last_touch_ms      INTEGER,
  switch_to          TEXT,
  listen_port        INTEGER,
  -- results
  exit_code          INTEGER,
  exit_signal        INTEGER,
  peak_footprint_bytes INTEGER,
  footprint_bytes    INTEGER,                      -- last sample, display
  footprint_fail_n   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX job_state ON job (state, queue, priority);
CREATE UNIQUE INDEX job_idempo ON job (project, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND state IN ('needs_estimate','queued','held');
CREATE UNIQUE INDEX job_profile_live ON job (profile)
  WHERE profile IS NOT NULL AND state IN ('queued','held');      -- one live row per profile

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
-- keys: schema_created_ms, morning_latch (YYYY-MM-DD), memsize_bytes, last_start_ms, drain (0/1)
```

On open: if `user_version == 0` and there are no tables, create everything and set it to 2. If `user_version == 2`, continue. Anything else → exit 78: "database schema version N is not supported; move <path> aside (do NOT do this while jobs are running)".

## 3. Job ids and leases (`ids.py`)

- Job id: `time.strftime("%Y%m%dT%H%M%S", localtime(now))` + `-` + `secrets.token_hex(2)`. On collision (the PK insert fails), retry with a new suffix, up to 5 times.
- Lease: `secrets.token_hex(32)`. Stored as `hashlib.sha256(token.encode()).hexdigest()`. Compare with `hmac.compare_digest`.

## 4. Transitions and owners

Each row in this table is **one function** in `store.py`. No other code issues `UPDATE job SET state=` or `held_reason=`. Every UPDATE includes the expected current state in its `WHERE`, and the function returns `rowcount == 1`.

| Function | From | To | Notes |
|---|---|---|---|
| `insert_job` | — | `queued` / `needs_estimate` | `submitted_ms = enqueued_ms = now`. Creates the job dir and checkpoint dir (0700) and writes `spec.json` (no env values) |
| `set_estimate` | `needs_estimate` | `queued` | Sets `estimate_s`, `queue` |
| `commit_admit` | `queued` | `held/spawning` (daemon) or `held/await_attach` (external) | Sets `held_ms`, clears `eligible_since_ms`. External: sets `lease_hash`. **The admission authority** |
| `record_shim` | `held/spawning` | `held/spawning` | `shim_pid` + start time |
| `begin_child` | `held/spawning` or `held/await_attach`, `pid IS NULL` | daemon: stays `held/spawning` with the pid set. external: `held/live` | Records the workload identity (pid, path, start sec/usec, pgid). **Daemon spawn:** `gate_released` stays 0. The daemon writes the gate byte only after this commits (07 §2). **External:** the launcher writes its own gate after `attach` returns OK, so this function sets `held_reason='live'`, `gate_released=1`, `started_ms=now`, `ready=1` (unless a profile `health_url` applies) in the same UPDATE |
| `mark_gate_released` | `held/spawning`, pid set, `gate_released=0` | `held/live` | Daemon spawn only, called right after a successful gate write: `gate_released=1`, `started_ms=now`, `ready=1` unless `health_url` applies. Invariant: `held/live` ⇔ the workload was allowed to exec |
| `request_checkpoint` | `held/live` (cooperative) | `held/checkpointing` | `stop_requested_ms`, `stop_reason` |
| `mark_spilled` | `held/checkpointing` | `held/spilled` | `checkpoint_failed=1`, `spilled=1`, `spill_reason` |
| `begin_stop` | `held/live` or `held/spilled` (only if `stop_reason == 'terminate'`) or `held/checkpointing` (only `on_timeout=kill` or terminate) | `held/stopping` | `stop_requested_ms`, `stop_reason` |
| `requeue_continuation` | `held/checkpointing` | `queued` | Exit 0 + READY. `attempt+1`, `resume=1`, `enqueued_ms=now`, clear pid/shim/identity/flags (keep `checkpoint_failed=0`), write `history` |
| `requeue_unstarted` | `held/spawning` or `held/await_attach` (pid not released) | `queued` | Spawn aborted before exec, or `release` with no attach. `attempt` unchanged |
| `requeue_restartable` | `held/stopping` (restartable, `stop_reason` in preempt/morning/pressure/protective/switch/drain/idle) | `queued` (preempt, morning, pressure, protective, switch) or `done` (idle, drain) | Restartable after exit. Idle unload of a resident ends the row (`done`, `end_reason=idle_unload`). `ensure` creates a new row next time. Write `history` |
| `finish` | `held/*` | `done` / `failed` / `cancelled` | From `exit.json` or the `finish` RPC. `cancel_requested` + any exit → `cancelled`. Write `history` |
| `mark_lost` | `held/*` | `lost` | Identity mismatch, or the process is gone without an exit record |
| `cancel_unstarted` | `needs_estimate`, `queued` | `cancelled` | `end_reason` |
| `mark_cancel_requested` | `held/*` | same | `cancel_requested=1` |
| `set_flags` | `held/*` | same | `over_limit`, `yield_requested`, `estimate_overrun`, `ready`, `footprint_bytes`, `footprint_fail_n`, `peak_footprint_bytes`, `last_touch_ms`, `hold`, `switch_to` |

`finish` mapping from an exit record:

| Record | State | `end_reason` |
|---|---|---|
| `cancel_requested = 1` | `cancelled` | `cancelled` |
| exit 0 | `done` | `exit` |
| exit ≠ 0 | `failed` | `exit` |
| killed by a signal | `failed` | `signal` (`exit_signal` set) |
| exit 111 and `gate_released = 0` | `requeue_unstarted` instead (the gate closed before exec) | — |

Cooperative special case: when `held_reason = checkpointing` and the exit is 0 **and** `READY` is valid (08 §1) → `requeue_continuation`. Exit 0 without `READY` → `done`.

## 5. Project rows

- On project-file load, upsert `(id, weight)`. Set `present=1`. Never reset `usage`.
- If a file disappears, set `present=0`. Queued jobs of a non-present project stay queued but aren't eligible (`blocked_by: project_missing`).
- `usage` update (06 §4) happens on every tick, in the same transaction as other per-tick flag updates. One `BEGIN … COMMIT` per tick for all bookkeeping keeps write volume small.

## 6. Retention (on startup and then daily)

- Delete `event` rows with `t_ms < now − retention_days`.
- Delete `offender` rows with `cleared_ms < now − retention_days`.
- For terminal jobs with `released_ms < now − retention_days`: delete the job dir (logs). Keep the `job` row and `history` forever (they're small). Keep the checkpoint dir only if the job is `cancelled` with READY present (the user may want it); otherwise delete it.
- `gsched gc --older-than 7d` runs the same routine with a custom age.

## 7. Invariants (assert in tests and in a debug `check_invariants()` the tick runs when `GSCHED_DEBUG=1`)

1. At most one `held` job with `gpu='exclusive'`. If one exists, no `held` job with `gpu='shared'`.
2. Count of `held` with `gpu='shared'` ≤ `gpu_shared_max`.
3. Σ `memory_bytes` of `held` ≤ `budget_bytes` + (the overshoot allowed only for jobs admitted before a config change lowered the budget).
4. No lock appears in two `held` rows.
5. `held/live` ⇒ `pid IS NOT NULL`.
6. `state != 'held'` ⇒ `pid IS NULL` or the state is terminal.
7. `lease_hash IS NOT NULL` ⇒ `spawn='external'`.
8. One live row per profile (enforced by the index).

## 8. Tests (PR 2)

`test_user_version_mismatch_exits_78`, `test_db_file_mode_0600`, `test_transition_wrong_state_returns_false`, `test_commit_before_return` (a separate connection sees the row right after the call returns), `test_idempotency_index_is_unique`, `test_profile_live_unique`, `test_retention_deletes_old_events`, `test_invariants_hold_after_random_ops` (property-style: 500 random valid operations on the fake, check invariants after each).
