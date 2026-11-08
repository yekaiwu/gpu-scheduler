# 05: Daemon loop and RPC

## 1. Startup sequence (`gsched daemon`)

1. `os.umask(0o077)`. Resolve `GSCHED_HOME`. Create the directories (01 §6).
2. Load config (exit 78 on invalid).
3. `get_platform()` (exit 78 on a platform or layout failure).
4. Open the store (exit 78 on a `user_version` mismatch).
5. Single-instance lock: `fcntl.flock(open("$H/run/daemon.lock", "a"), LOCK_EX | LOCK_NB)`. If that fails, exit 75 with "another gsched daemon is running (pid from daemon.pid)". launchd's `KeepAlive` will retry, which is fine.
6. Record `meta.memsize_bytes`. If it changed since last start (RAM upgrade or a different machine), log `memsize_changed`.
7. Load the project and profile directories.
8. **Reconcile** (07 §6) **before listening**, so no client sees a stale view.
9. Retention (04 §6).
10. Bind the socket: if the path exists, `unlink` it (the lock in step 5 proves no live daemon owns it). Then `bind`, `chmod 0600`, `listen(64)`, `setblocking(False)`.
11. Write `daemon.pid` (`pid start_sec`).
12. Install signal handlers: `SIGTERM`/`SIGINT` → set `stopping=True` (the loop exits after the current iteration). `SIGHUP` → reload config. `SIGCHLD` → no handler (reap on the tick). Use `signal.set_wakeup_fd` with a self-pipe registered in the selector, so a signal wakes `select` promptly.
13. Enter the loop.

On exit: close the listener, unlink the socket and `daemon.pid`, and close the DB. **Do not signal any job.** Running jobs continue under their shims, and the next daemon start reconciles them.

## 2. The loop

```python
class Daemon:
    def serve_forever(self):
        while not self.stopping:
            self.step(timeout=self._select_timeout())

    def step(self, timeout: float):
        for key, ev in self.sel.select(timeout):
            kind = key.data.kind
            if kind == "listen": self._accept()
            elif kind == "conn": self._on_conn_readable(key.data)
            elif kind == "report": self.supervisor.on_report_readable(key.data)   # shim pid reports (07)
            elif kind == "wakeup": self._drain_wakeup()
        self._expire_idle_connections()
        if self.clock.mono() >= self.next_tick_mono:
            self.tick()                                    # 06
            self.next_tick_mono = self.clock.mono() + 1.0
        elif self.admit_hint:
            self.tick(admission_only=True)                 # cheap: no scan, reuse the last snapshot
        self.admit_hint = False

    def _select_timeout(self):
        return max(0.0, min(1.0, self.next_tick_mono - self.clock.mono()))
```

- `admit_hint` is set by `submit`, `cancel`, `estimate`, `ensure`, `finish`, `release`, and by a shim exit. It lets a job start within milliseconds when capacity is already free. `tick(admission_only=True)` runs phases 0, 1, and 7 of 06 using the last memory sample, with the `pending` correction recomputed. It is not allowed to signal anything.
- Tests drive `step(timeout=0)` and `clock.advance(1)`.

## 3. Connections and framing

```python
@dataclass
class Conn:
    sock: socket.socket
    uid: int
    peer_pid: int
    buf: bytearray
    opened_mono: float
    parked: Parked | None = None     # set by wait
```

- Accept: `sock.setblocking(False)`. `peer_credentials` → if the uid ≠ `os.getuid()`, send `forbidden` and close. Register for `EVENT_READ`.
- Read: `recv(65536)`, append to `buf`. If `len(buf) > 1 MiB` → error `invalid` ("request too large") and close. When `b"\n"` is in `buf`: split off the first line, `json.loads(line)` (with `parse_constant` rejecting NaN), and dispatch. **One request per connection.** Bytes after the first newline are ignored. EOF before a newline → close silently.
- Write: responses are small. Use `sock.setblocking(True)`, `settimeout(1.0)`, `sendall(json.dumps(resp, separators=(",",":")).encode() + b"\n")`, then close, unless the request parked. If `sendall` times out (a stuck client), close and log `client_write_timeout`. A parked waiter whose write fails is treated as gone (§6).
- Idle timeout: a connection that hasn't sent a full request within 10 s is closed.
- Max 64 concurrent connections. Beyond that, accept and close immediately with `{"error":{"code":"busy"}}`.

### Request and response envelope

```json
{"rpc":"gpu-scheduler.rpc.v1","method":"submit","params":{...},"door":"cli"}
```

```json
{"rpc":"gpu-scheduler.rpc.v1","ok":true,"result":{...}}
{"rpc":"gpu-scheduler.rpc.v1","ok":false,"error":{"code":"unknown_project","message":"...","detail":{...}}}
```

`door` is `cli`, `mcp`, or `launcher`, as declared by the client. It's self-reported, so it is a **policy hint, not a security boundary**: a same-user process can claim to be `cli`. That's acceptable because the same user can run `gsched` directly. The MCP permission gate protects against an agent that only has MCP tools (it can't choose the door). Document this honestly in the README.

## 4. Methods

All methods return within one loop iteration except `wait` and `get` with `wait_s`, which park.

| Method | Params | Result | Notes |
|---|---|---|---|
| `submit` | `spec`, `door` | `{job, state, queue, warnings, idempotent?}` or the observe result | 03 §5 |
| `get` | `job`, `wait_s?` (≤ `mcp_max_wait_s` for door mcp, ≤ 300 otherwise) | job view (12 §4) | With `wait_s`, parks until the state or `held_reason` changes, or until timeout. Then returns the current view |
| `list` | `state?` (str or list), `project?`, `limit?` (default 100, max 1000) | `{jobs: [view…]}` newest first | Views omit `command` and `env` (use `get` for the command) |
| `wait` | `job`, `until` = `exit` (default) \| `start` | job view | `start` only for `spawn: external`, and its result includes `lease` exactly once. `exit` returns when the state is terminal. There's no timeout server-side; the CLI implements `--timeout` by closing the socket |
| `cancel` | `job` or `lease`, `terminate?`, `end_reason?` | `{job, state, action}` | §5. `end_reason` may only be `max_wait` (sent by `gsched exec --max-wait`): it becomes the job's `end_reason` when the cancel ends an unstarted job (`queued`, or external `await_attach` without a pid), and the daemon logs an `exec_max_wait` event |
| `estimate` | `job`, `estimate_s` | view | `needs_estimate → queued`. Also allowed on `queued` (re-assigns the queue) |
| `logs` | `job`, `stream` = `stdout`\|`stderr`, `tail_bytes?` (default 8192, max 1 MiB; MCP max 16384), `offset?` | `{data, offset, eof, size}` | Reads the job's log file. `data` is decoded UTF-8 with `errors="replace"`. `offset` supports `--follow` polling |
| `status` | `brief?` | 12 §3 | Never mutates. May call pure policy. Includes the `reservations` array; with `door: "dashboard"` each entry also carries `windows` (every window overlapping now ± 36 h) for the timeline shading |
| `ensure` | `profile` | resident view | 08 §3 |
| `inhibit` / `uninhibit` | `profile` | `{profile, inhibited}` | |
| `hold` / `unhold` | `profile` | `{profile, hold}` | Sets the profile flag and the live row's `hold` |
| `drain` / `undrain` | — | `{draining}` | Drain: refuse `submit` with `draining`, inhibit all profiles, unload idle residents (hold = 0) now, let everything else finish. Persisted in `meta.drain` |
| `attach` | `lease`, `pid` | `{job, state:"held", held_reason:"live"}` | External only. Validates the lease hash, that the job is `held/await_attach`, that pid ∉ {0, 1}, that `proc(pid)` exists with our uid, and that it isn't the daemon itself. Calls `begin_child`. The caller writes its gate byte only after an OK |
| `finish` | `lease`, `exit_code?`, `signal?` | `{job, state}` | External only. Finalizes like `exit.json`. Idempotent: a second call returns the terminal state |
| `release` | `lease` | `{job, state}` | External only. No pid attached means the launcher decided not to run, so the state becomes `cancelled` with `end_reason=released_unstarted` (store function `finish` with that reason). Pid attached and alive → error `still_running`. Pid attached and gone → `lost` (`end_reason=exit_unknown`), since the launcher should have called `finish` |
| `suggest` | `project`, `name?` | `{memory_gib_p95, peak_gib_max, runtime_s_p50, runtime_s_p95, n}` | 12 §6 |
| `doctor` | — | checks run **inside the daemon process** (TCC is per-process) | 11 §4 |
| `reload` | — | `{ok}` | Re-reads config, projects, profiles |
| `ping` | — | `{version, pid, enforce, uptime_s}` | Cheap, for health checks |
| `events` | `kinds?`, `exclude_kinds?`, `since_ms?` (default now − 6 h), `job?`, `limit?` (default 2000, max 10000), `newest_first?` | `{events:[{t_ms, kind, job, body}]}` oldest first unless `newest_first` | Read-only (15). `list` also takes `since_ms` (terminal jobs released since then plus all non-terminal) |
| `gc` | `older_than_s?` | `{events, offenders, job_dirs}` | Retention now (04 §6) |

The latent-alpha-style flow from the v1 design that needs "release without attach, then retry later" maps to: `release` (→ `cancelled`) and submit again later. Retrying means a new job id. That's simpler and keeps one meaning per state.

## 5. Cancel

| Current | `cancel` | `cancel terminate=true` |
|---|---|---|
| `needs_estimate`, `queued` | `cancel_unstarted` → `cancelled`. Also cascades `dependency_failed` to dependents on the next tick | same |
| `held/spawning` (gate not released) | Kill the shim group (ours, nothing has exec'd) → `cancelled` when reaped | same |
| `held/await_attach` (external, no pid) | → `cancelled` immediately. The lease becomes invalid | same |
| `held` restartable (ours) | `begin_stop(cancel)`: TERM→KILL group → `cancelled` | same |
| `held` cooperative (ours, live) | `mark_cancel_requested` + `request_checkpoint` (one USR1). When it exits → `cancelled` (keep its checkpoint dir) | `begin_stop(terminate)`: TERM→KILL group |
| `held` cooperative `checkpointing` | `mark_cancel_requested` only | `begin_stop(terminate)` |
| `held` atomic or spilled (ours) | `mark_cancel_requested` only. Reply `action: "cancel_requested_no_signal"` and a message: "atomic jobs are not signaled; use terminate=true to kill it (unsaved work is lost)" | `begin_stop(terminate)`: TERM→KILL group |
| `held` external (any) | `mark_cancel_requested`. Never signaled. The launcher sees the flag via `get` | `forbidden`: "external jobs are owned by their launcher" |
| terminal | `bad_state` | `bad_state` |

MCP `terminate=true` requires the project's `mcp_terminate: true`, else `forbidden`.

## 6. Parking and peer liveness

```python
@dataclass
class Parked:
    kind: Literal["wait_exit", "wait_start", "get_wait"]
    job: str
    deadline_mono: float | None       # get_wait only
    last_seen: tuple[str, str | None] # (state, held_reason) at park time
```

- On every tick (and after every state change), `notify_parked(job_id)` checks each parked connection on that job. It replies and closes when the condition is met.
- Peer liveness on each tick for parked connections: `platform.pid_alive(conn.peer_pid)`. If it's false, or the socket is readable with EOF (check with `recv(1, MSG_PEEK)` returning `b""`), the waiter is gone.

| Parked kind | Waiter gone → |
|---|---|
| `wait_exit`, `get_wait` | Unpark and close. **No state change.** A CLI that exits never cancels a job |
| `wait_start` before the lease was sent | If the job is `queued`: no state change, but it is no longer eligible (an external job needs a parked start waiter, 06 §2). If no other waiter re-parks within `attach_grace_ms`, `cancel_unstarted` with `end_reason=waiter_gone` |
| lease already sent, no `attach` yet | Keep `held/await_attach` for `attach_grace_ms` from the lease time, then `cancelled` with `end_reason=attach_timeout`. The slot frees |

- `wait --until start` on a `spawn: daemon` job → `invalid`.
- External jobs: `commit_admit` happens only when a start waiter is parked (06 §2). The lease is sent in the reply to that waiter **after** `commit_admit` returns.

## 7. Tests (PR 3)

`test_commit_before_response` (the fake socket client sees the response only after a second sqlite connection already sees `held`), `test_two_submits_one_exclusive_holder`, `test_observe_inserts_nothing`, `test_observe_explains_would`, `test_socket_close_after_submit_does_not_cancel`, `test_parked_wait_start_cancels_after_grace_when_peer_dies`, `test_wait_exit_peer_death_no_state_change`, `test_external_wait_start_returns_lease_once`, `test_attach_rejects_pid_0_1_and_self`, `test_release_without_attach_cancels`, `test_finish_idempotent`, `test_get_wait_returns_on_change`, `test_get_wait_capped_for_mcp`, `test_request_too_large`, `test_peer_uid_mismatch_forbidden`, `test_idle_connection_closed`, `test_socket_path_too_long_exits_78`, `test_single_instance_lock`, `test_shutdown_does_not_signal_jobs`, `test_unknown_project_no_row_exit_2`, `test_cancel_atomic_is_flag_only`, `test_cancel_terminate_atomic_kills_group`, `test_mcp_terminate_forbidden_by_default`, `test_drain_refuses_submit`.
