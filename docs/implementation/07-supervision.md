# 07: Supervision

`gsched/supervise.py` (daemon side) and `gsched/shim.py` (one process per running job).

## 1. Process tree

```
launchd
 └─ python -m gsched daemon
     └─ python -I -m gsched.shim <job_id>  (session leader; pgid = shim pid)   ← daemon's direct child
         └─ workload (fork → wait on gate → execvpe(command))                  ← pgid = shim pid
             └─ whatever the workload starts (same pgid unless it calls setsid)
     └─ /usr/bin/caffeinate -i -w <shim pid>                                    ← one per job, optional
```

Why a shim:

- It survives the daemon. If the daemon crashes or restarts, the shim keeps waiting for the workload and records the exit code in `exit.json`. Without it, the exit status of a reparented child is lost (review C-2).
- It gives every job one process group to measure and to signal.
- Its pid and start time are a second identity anchor.

## 2. Spawn sequence (daemon spawn)

Preconditions: `commit_admit` has committed `held/spawning`.

```python
def spawn(self, job):
    gate_r, gate_w = os.pipe()          # daemon keeps gate_w; workload reads gate_r
    rep_r, rep_w = os.pipe()            # shim writes the workload pid to rep_w; daemon reads rep_r
    os.set_inheritable(gate_r, True); os.set_inheritable(rep_w, True)
    argv = [cfg_python, "-I", "-m", "gsched.shim",
            "--job", job.id, "--gate-fd", str(gate_r), "--report-fd", str(rep_w),
            "--exit-file", jobdir/"exit.json", "--"] + job.command
    try:
        shim_pid = platform.spawn_shim(argv, env=job_env(...), cwd=job.cwd,
                                       stdout_path=job.stdout_path, stderr_path=job.stderr_path,
                                       pass_fds=(gate_r, rep_w))
    except OSError as e:
        close all four fds; store.finish(job.id, state="failed", end_reason="spawn_error", detail=str(e)); return
    os.close(gate_r); os.close(rep_w)
    ident = platform.proc(shim_pid)  → store.record_shim(job.id, shim_pid, ident.start_sec, ident.start_usec)
    os.set_blocking(rep_r, False)
    self.pending[job.id] = PendingSpawn(gate_w, rep_r, deadline=mono + spawn_report_timeout_ms/1000)
    self.sel.register(rep_r, EVENT_READ, data=Key("report", job.id))
```

`cfg_python` is `sys.executable` of the daemon (the install venv's interpreter). The shim imports only `gsched.shim` and the stdlib. Keep it import-light: `os`, `sys`, `json`, `signal`, `time`, `ctypes` for the optional peak footprint. **Do not import `gsched.daemon` or `gsched.store`.**

`on_report_readable(job_id)`:

```python
data = os.read(rep_r, 64)                       # "12345\n"
pid = int(data.strip())
ident = platform.proc(pid)                      # must exist, ppid == shim_pid, pgid == shim_pid, uid == ours
if not ident or ident.ppid != shim_pid or ident.pgid != shim_pid:
    abort_spawn(job, reason="bad_report"); return
ok = store.begin_child(job.id, JobIdentity(pid, ident.start_sec, ident.start_usec, ident.path, ident.pgid))
if not ok: abort_spawn(job, reason="begin_child_failed"); return
try:
    os.write(gate_w, b"1")                      # COMMIT happened above; this releases exec
except OSError:
    abort_spawn(job, reason="gate_write_failed"); return
finally:
    os.close(gate_w); unregister + close rep_r
store.mark_gate_released(job.id, now)
if cfg.prevent_sleep: platform.caffeinate(shim_pid)
```

`abort_spawn`: close `gate_w` **without writing**. The workload reads EOF and exits 111 without exec'ing (§3), the shim records it, and on reap the row goes through `requeue_unstarted`. If the shim doesn't exit within 5 s, `signal_group(shim identity, SIGKILL)`. That's safe because nothing has exec'd.

`expire_spawn_and_attach` (each tick): any `PendingSpawn` past its deadline → `abort_spawn(reason="report_timeout")`.

**The daemon never changes directory into the job's cwd (16 addendum).** Under launchd, `chdir()` into `~/Documents` etc. without TCC approval blocks, and `subprocess.Popen(cwd=...)` blocks its *parent* until the child execs. So the shim is started with `cwd = $H/jobs/<id>` and gets the job's cwd as `--cwd <dir>`:

```
shim ... --exit-file F [--cwd DIR] -- command...
workload (after the gate byte "1"): chdir(DIR)  -> on OSError: stderr line, exit 126
                                    write "cwd_ok\n" to the report pipe, close it, then execvpe
```

The workload keeps its copy of the report pipe after the pid line (the shim parent closes its own). After writing the gate byte the daemon does **not** close the read end: it records a `CwdWait` (deadline = now + `spawn_report_timeout_ms`) and registers nothing new, the fd stays in the selector. `cwd_ok` (or EOF, meaning the workload exited, e.g. exit 126) ends the wait. If the deadline passes first, event `cwd_blocked` and `Supervisor.stop(job, "cwd_blocked")`: `begin_stop` + SIGTERM to the group, SIGKILL after `kill_grace_s` like any stop. This is allowed for atomic and cooperative jobs too, because nothing of the user's command has exec'd. Finalization: `failed` with `end_reason = cwd_blocked`. The wait is lost on a daemon restart (the job then runs unprotected, as before this change).

Note on `ident.path` at report time: the workload hasn't exec'd yet, so `proc_pidpath` returns the **Python** path. After exec, the path changes to the command's binary. So the stored `pid_path` is refreshed on the first tick after `gate_released`: read `proc_pidpath(pid)` and, if the start time (sec and usec) still matches, update `pid_path`. Identity checks before that refresh compare start time and pgid only. A **later** exec by the same process (a wrapper script that ends in `exec llama-server`, `/usr/bin/env`, and on macOS `/bin/sh` itself, which execs `/bin/bash`) keeps the pid and start time too: it is the same process, so the path is refreshed again on the next tick with event `pid_path_changed`, never `lost`. A pid whose start time differs is a different process (`lost`, no signal). Document this in the code: it's the one place where the path is allowed to change.

## 3. The shim (`gsched/shim.py`)

```python
def main(argv):
    a = parse(argv)                     # --job --gate-fd --report-fd --exit-file -- command...
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2):
        signal.signal(s, _noop)          # the shim survives group signals; handlers reset to default on exec in the child
    pid = os.fork()
    if pid == 0:                         # workload
        os.close(a.report_fd)
        for s in (...same...): signal.signal(s, signal.SIG_DFL)
        b = os.read(a.gate_fd, 1)        # blocks until the daemon releases or closes
        os.close(a.gate_fd)
        if b != b"1": os._exit(111)
        try: os.execvpe(a.command[0], a.command, os.environ)
        except OSError as e:
            os.write(2, f"gsched: exec failed: {e}\n".encode()); os._exit(127)
    os.close(a.gate_fd)
    os.write(a.report_fd, f"{pid}\n".encode()); os.close(a.report_fd)
    peak = None
    while True:
        try:
            # WNOWAIT: inspect before reaping so the zombie's rusage is still readable
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            break
        except InterruptedError: continue
        except ChildProcessError: info = None; break
    peak = _peak_footprint(pid)          # proc_pid_rusage V4 lifetime max on the zombie; None on failure
    _, status = os.waitpid(pid, 0)
    rec = {"v":1, "job":a.job, "pid":pid, "t_ms":now_ms(), "peak_footprint_bytes":peak}
    if os.WIFEXITED(status): rec["exit_code"] = os.WEXITSTATUS(status)
    elif os.WIFSIGNALED(status): rec["signal"] = os.WTERMSIG(status)
    _atomic_write_json(a.exit_file, rec)  # write tmp, fsync, rename, fsync dir
    os._exit(rec.get("exit_code", 128 + rec.get("signal", 0)) & 0xFF)
```

- `os.waitid` with `WNOWAIT` is available on macOS in Python 3.12. If `_peak_footprint` doesn't work on zombies on a given macOS version, the daemon falls back to the max footprint it observed (it tracks `peak_footprint_bytes` per tick). A live test checks which case applies and logs it.
- Python's handlers run in the shim's main thread. The `_noop` handlers keep `SIGTERM`-to-group from killing the shim before it records the exit. The shim can still be killed by `SIGKILL`. In that case `exit.json` is missing → `lost` (or, if the workload is still alive, it's reattached by identity at reconcile; see §6).
- Process group signals reach the shim too (it's in the group). That's harmless because of the no-op handlers.
- The shim doesn't forward signals: the daemon signals the workload pid (`SIGUSR1`) or the whole group (`SIGTERM`/`SIGKILL`) directly.
- **Exit code 111** is reserved for "gate closed before exec". If a user command legitimately exits 111, the row has `gate_released = 1`, so it's not misread. The rule in 04 §4 requires `gate_released = 0`.

## 4. Reaping and finalization (`reap_and_finalize`)

Each tick:

1. `platform.reap()` → `waitpid(-1, WNOHANG)` in a loop. For each pid, if it's a known shim, mark the shim as exited. If it's a caffeinate, ignore it.
2. For each `held` daemon-spawn job whose shim has exited (reaped now, or `not identity_matches(shim)` after a restart):
   - Read `exit.json`. If it's valid and `rec.job == job.id`:
     - `held_reason == "checkpointing"` and `exit_code == 0` and READY valid (08 §1) → `requeue_continuation`.
     - `exit_code == 111` and `gate_released == 0` → `requeue_unstarted`.
     - `stop_reason` in the restartable requeue set → `requeue_restartable`.
     - else → `finish` (04 §4 mapping).
   - Missing or invalid → `mark_lost(end_reason="exit_unknown")`.
   - Rename `exit.json` → `exit.<attempt>.json` so a continuation's later record isn't confused with this one.
3. `peak_footprint_bytes` = max(record peak, observed peak). Insert a `history` row (12 §6).
4. `admit_hint = True`.

## 5. Identity checks and job trees (`check_identities`, scan)

Each tick, for each `held` job with a pid:

- `identity_matches(pid identity)` is false **and** the shim has not exited yet → the workload exited and the shim will report shortly. Do nothing this tick.
- Workload identity false **and** shim identity false **and** no `exit.json` → `mark_lost(identity_mismatch)`. No signal.
- Shim identity false, no `exit.json`, gate released, **workload identity still true** (the shim was killed or jetsammed while the daemon runs): same as the reconcile case in §6. Keep the job `held` with `shim_pid` NULL (its memory is still in use and it can still be stopped), event `shim_lost`. When the workload goes, `lost` (`exit_unknown`).

**Job tree** (for footprint and for "ours" classification): the set of pids with `pgid == job.pgid`, plus the transitive descendants of the shim pid found through `ppid` links in the same scan (this catches children that called `setsid`). Build a `children: dict[ppid, list[pid]]` index once per scan. Footprint = Σ `footprint_bytes` over the tree. If the workload pid's own footprint is None, the job footprint is None (`footprint_fail_n += 1`), even if other members are readable. If the read succeeds, `footprint_fail_n = 0`.

Track the per-job `peak_footprint_bytes = max(previous, current tree sum)`.

## 6. Reconciliation at startup

Before listening:

```
for job in store.held_jobs():
    if job.spawn == "external":
        if job.pid is None:       → cancelled (end_reason=daemon_restart_before_attach); the lease is invalid
        elif identity_matches:    keep held/live (the launcher still owns it)
        else:                     → lost (exit_unknown)
        continue
    exit = read exit.json
    if exit valid:                finalize as in §4
    elif shim identity matches:   keep; the shim still waits. (reap won't work since it's not our child; poll identity each tick)
                                  if held_reason == "spawning" and gate_released == 0:
                                      the gate write end died with the old daemon → the workload exits 111 → the shim writes exit.json → finalize next tick
    elif workload identity matches (the shim died, the workload lives):
                                  keep as held/live with shim_pid NULL. Exit status will be unknown → when it goes, lost.
                                  event "shim_lost". Signals are still allowed (identity-checked)
    else:                         → lost (exit_unknown)
```

- Jobs that survived the restart are no longer the daemon's children, so `waitpid` won't see them. Exit detection for them is "shim identity no longer matches" + `exit.json`. Implement exit detection that way **always**, with `reap()` used only to clean up zombies. That makes the post-restart path identical to the normal one.
- Re-run caffeinate for surviving jobs if `prevent_sleep`.
- Parked connections don't survive a restart. Clients reconnect: `gsched wait` retries the connection for up to 30 s on `ECONNREFUSED`/`ENOENT` before exiting 3.

## 7. Signals (all through the platform)

| Intent | Call | Target |
|---|---|---|
| Checkpoint request | `signal_pid(workload, cfg.checkpoint_signal)` | workload pid only |
| Stop (TERM) | `signal_group(workload, SIGTERM)` | whole job group |
| Escalation (KILL) | `signal_group(workload, SIGKILL)` | whole job group |
| Descendants that left the group | after the group signal, also `signal_pid` each tree member with `pgid != job.pgid` whose identity is from this tick's scan | stops escapees on TERM/KILL. Never used for USR1 |

Every send writes an `event` of kind `signal`: `{job, pid|pgid, sig, reason}`. Tests assert on these events and on `FakePlatform.signals`.

## 8. External launchers

The external path doesn't use the shim. The launcher:

1. `gsched submit --file spec.json` (with `spawn: external`) → id.
2. `gsched wait ID --until start` → blocks. When admitted, stdout is `{"job":…, "lease":"…", "held_reason":"await_attach"}`.
3. Spawn its own child blocked on its own gate (the snippet below).
4. `gsched attach --lease L --pid P` → exit 0 on OK. On failure: kill the child, don't release its gate, `gsched release --lease L`.
5. Release its gate (write one byte).
6. When the child exits: `gsched finish --lease L --exit-code N`.
7. Recommended: in a `finally`, run `gsched release --lease L`. It's a no-op on terminal rows (it returns the state).

A copyable Python helper ships as `gsched/launcher.py` (stdlib only, importable by other projects, or vendorable as one file):

```python
with gsched.launcher.admitted(spec) as lease:          # submit + wait --until start
    child = lease.spawn(argv, cwd=..., env=...)          # gate, attach, release-gate; kills on attach failure
    code = child.wait()
    lease.finish(code)
```

The daemon never signals external jobs. `limit_s` on an external job only sets `yield_requested`. The launcher can poll `get` and act on it.

## 9. caffeinate

`prevent_sleep: true` starts `caffeinate -i -w <shim_pid>` after the gate release. One per job. It ends on its own when the shim exits. The daemon tracks those pids only for reaping. `status` shows `sleep_prevented: true` while any are alive. This doesn't prevent sleep when the lid closes on a laptop on battery. Document that.

## 10. Tests

PR 3 (fake): `test_attach_before_gate_release` (the gate byte is written only after `begin_child` commits, checked with a second DB connection inside a wrapped `os.write`), `test_spawn_error_marks_failed_slot_free`, `test_report_timeout_aborts_and_requeues`, `test_gate_write_failure_requeues_unstarted`, `test_exit_111_unreleased_is_requeue_not_failure`, `test_exit_111_after_release_is_failure`, `test_exit_json_finalizes`, `test_missing_exit_json_is_lost`, `test_identity_mismatch_lost_no_signal`, `test_pid_path_refreshed_once_after_exec`, `test_job_footprint_sums_tree_including_setsid_child`, `test_descendant_llama_server_is_ours_not_foreign`.

PR 3 live (`@pytest.mark.live`, real shim + `/bin/sh -c`): `test_live_shim_records_exit_code`, `test_live_shim_survives_group_sigterm_and_records_signal`, `test_live_gate_close_exits_111_without_exec` (the command would create a file; assert it doesn't exist), `test_live_reconcile_after_daemon_restart` (start a daemon, spawn `sleep 3`, stop the daemon, start a new one, assert `done` with exit 0), `test_live_peak_footprint_recorded`.

## 11. Process scan and classification (`gsched/scan.py`)

The scan runs once per full tick, over `platform.processes()`.

```python
@dataclass(frozen=True)
class ScanResult:
    trees: dict[str, frozenset[int]]          # job id -> pids in its tree
    job_footprint: dict[str, int | None]      # job id -> summed bytes (None if the workload is unreadable)
    foreign_heavy: list[Offender]             # basename or port matches, not ours
    orphan_shims: list[Offender]
    noted: list[Offender]                     # footprint > footprint_note_gib, not ours, not heavy
    ports_listening: dict[int, bool]          # last probe result per configured port
```

Algorithm:

1. Build `by_pid` and `children[ppid]` from the process list.
2. For each `held` job with a pid: its tree = pids with `pgid == job.pgid` ∪ descendants of `shim_pid` (or of `pid` if the shim is gone), found by BFS over `children`. Mark them all as `ours`. The shim and the job's caffeinate are `ours` too.
3. Also mark as `ours`: the daemon itself and its descendants that aren't in a job (for example, a probe subprocess).
4. For every other pid with `uid == os.getuid()` **or** a readable path:
   - `basename(path) in heavy_basenames` → `foreign_heavy` (any footprint).
   - path or argv indicates `gsched.shim` (path is the venv python **and** `pbi_comm` starts with `python`, then confirm with `KERN_PROCARGS2` only for those candidates; if argv can't be read, skip) and no job row has that shim pid → `orphan_shim`.
   - `footprint > footprint_note_gib` → `noted`.
   - Processes of other users (root daemons, `_windowserver`) are only ever `noted`, and only if their footprint is readable (it usually isn't). `heavy_basenames` also applies to other users' processes when the path is readable. A system-wide Ollama install running as another user still counts.
5. Ports (every `port_probe_s`): for each port in `heavy_listen_ports`, `listening = platform.port_listening(port)`. If it's listening and **no** running job of ours declares `listen_port == port` → `foreign_heavy` (pid looked up with `lsof` once when it flips; otherwise null).
6. Return the result. **The scan never signals.**

`KERN_PROCARGS2` (argv reading) via `sysctl([CTL_KERN, KERN_PROCARGS2, pid])`, using `libc.sysctl` with an int MIB array. Only needed for the orphan-shim check. If you'd rather skip it in v1, an orphan shim is still caught indirectly when its workload's basename is heavy. Mark it as optional in PR 2 and implement it in PR 5.

**Protective mode** (state kept in memory and mirrored to `meta.protective`):

- Enter when `foreign_heavy ∪ orphan_shims ∪ gpu_busy_foreign` is non-empty. `gpu_busy_foreign` (experimental, only when `gpu_busy_foreign_pct` is set): device util > X for 30 s with no `gpu != none` job of ours running.
- Exit when that set has been empty for `protective_clear_s` continuously.
- Offender rows: insert on first sight (keyed by `(pid, start_sec)` or by `port`), update `last_seen_ms` each tick, and set `cleared_ms` when gone. Use one row per offender instance, not per tick.

Tests (PR 2 for classification, PR 5 for protective actions): `test_browser_footprint_is_record_only`, `test_foreign_heavy_any_footprint_protective_not_signaled`, `test_foreign_port_listener_is_heavy`, `test_our_listen_port_is_not_foreign`, `test_descendant_llama_server_is_ours_not_foreign`, `test_protective_clears_after_30s`, `test_protective_blocks_gpu_not_cpu_jobs`, `test_protective_unloads_our_restartable_gpu_only`, `test_offender_one_row_per_instance`, `test_other_user_heavy_basename_counts`.
