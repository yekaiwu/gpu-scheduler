# 16: `gsched exec`, time reservations, hardening (PR 12)

These were added after the first live review. The motivation: integrating existing LaunchAgent-driven pipelines without editing them, and protecting scheduled windows that other jobs keep colliding with. The observed failure was a nightly pass skipping with "available 18.5 GB < 28.8 GB" because something else was holding memory when the pass came due.

## 1. `gsched exec`: wrap any existing command as an external job

```
gsched exec [--project P] [--name N] --mem GIB [--gpu M] [--cpu N] [--lock L]... --checkpoint MODE
            [--est DUR] [--limit DUR] [--priority N] [--queue auto|day|night]
            [--max-wait DUR] [--on-max-wait run|fail] [--fail-open | --fail-closed] -- argv...
```

The wrapper is the launcher (`spawn: external`). It's designed to be placed in front of an existing LaunchAgent's `ProgramArguments` or a cron line without changing the program:

1. Build the spec the same way as `gsched run`, with `spawn: external` and `cwd = os.getcwd()`. `env` isn't sent: the child inherits the wrapper's environment exactly as before.
2. `submit` (a missing or refused socket is retried for `--connect-grace`, default 20 s, so a daemon that is restarting or starting at login is not bypassed). Results:
   - Connection error (daemon down), or `state: observe` → **fail-open default**: `os.execvp(argv[0], argv)`. The process tree is then identical to running without the wrapper. With `--fail-closed`, exit 75 instead.
   - Request error (bad spec, unknown project, `too_big`) → print it to stderr. Fail-open: exec anyway, with a loud stderr line `gsched: exec fallback: <reason>`. Fail-closed: exit 2.
3. Park on `wait --until start`, holding the socket open. While waiting, if SIGTERM/SIGINT/SIGHUP arrives (for example `launchctl bootout`) → send `cancel` for the job and exit 143/130/129 without running the command.
4. `--max-wait` (default: none): if the start hasn't arrived by then → `cancel` the job, then follow `--on-max-wait`: `run` (default with fail-open) execs the command directly, or `fail` exits 75. Stderr says which happened. In both cases an event `exec_max_wait` is logged by the daemon from the cancel's `end_reason=max_wait`.
5. Got the lease → spawn the child using the gated pattern (`gsched.launcher`): pipe gate, `start_new_session=False, process_group=0` (keep the wrapper's session, but the child **leads its own process group**; *changed after live testing 2026-11-08*: sharing the wrapper's group delivered a group-wide SIGTERM twice, once directly and once forwarded, and llama-server aborted on the "second interrupt". The wrapper now forwards every signal exactly once. Trade-off: if the wrapper itself is SIGKILLed, the child keeps running and becomes a foreign process, which protective mode handles), and stdio inherited (stdout/stderr go wherever launchd or the terminal pointed them). `attach` → release the gate.
   - The child is an ordinary child of the wrapper, so launchd's `bootout` signals the whole job's process group as usual. The wrapper also forwards SIGTERM/SIGINT/SIGHUP/SIGUSR1/SIGUSR2/SIGQUIT to the child pid.
6. `waitpid` the child, then `finish` with the exit code or signal, and **exit with the child's exact status**: the same code, or re-raise the same signal on itself after resetting it to SIG_DFL. That keeps launchd `KeepAlive` and `SuccessfulExit` semantics and callers' rc checks unchanged.
7. If the daemon becomes unreachable after attach, keep waiting on the child. `finish` is retried 3 times. On failure, exit with the child's status anyway: the daemon will mark the job `lost` when it sees the pid gone, which is accepted.

The daemon never signals the wrapped process (it's external). Memory accounting uses the attached pid's tree. Since the child is in the wrapper's process group (not its own), the job tree for external jobs is the **descendants of the attached pid** (ppid BFS), not the pgid. Make sure `scan.py` handles `spawn: external` that way, so a launchd job's unrelated group members aren't counted.

Tests: `test_exec_failopen_when_daemon_down_execs_directly`, `test_exec_observe_mode_execs_directly`, `test_exec_failclosed_exit_75`, `test_exec_propagates_exit_code`, `test_exec_propagates_signal_death`, `test_exec_sigterm_while_queued_cancels_and_never_runs`, `test_exec_max_wait_run`, `test_exec_max_wait_fail`, `test_exec_forwards_sigterm_to_child`, `test_exec_external_tree_is_descendants_not_pgid`, and live `test_live_exec_wraps_real_command_end_to_end`.

### 1b. `--replace` (in-place), recommended for LaunchAgents

After step 4, instead of spawning a child: `attach` with the wrapper's own pid, restore signal dispositions, and `os.execvp(argv)`. The launchd job's process *becomes* the command. Use it whenever the command (or its dylibs or venv) lives under a TCC-protected folder, or when launchd signal semantics must be exactly preserved. The exit status isn't reported. The daemon ends the job `done/exit_unobserved` when the pid disappears. See QUESTIONS #40.

## 2. Time reservations

Config key `reservations` (default `[]`):

```json
"reservations": [
  {"name": "t233-pass", "days": ["mon","tue","wed","thu","fri"], "start": "19:00", "end": "23:30",
   "project": "latent-alpha", "memory_gib": 27, "gpu": "exclusive", "unload_restartable": true}
]
```

- `days`: three-letter lowercase weekday names, or `"daily"`. `start`/`end` are `HH:MM` in the config time zone. If `end <= start`, the window wraps midnight (the `days` entry is the day it **starts**).
- Validation: name unique, `^[a-z0-9-]{1,40}$`. `project` must be a configured project at use time (if it's missing, the reservation still applies; it just has no owner jobs). `memory_gib > 0`, `gpu` in `exclusive|shared|none`.

Semantics (pure; in `policy/reservations.py`):

- `active(now)` = the reservations whose window contains `now`.
- `upcoming(now, horizon_ms)` = windows starting within the horizon.
- For a candidate job **not** belonging to the reservation's project, the reservation **applies** when the window is active, **or** when `now + (estimate_s or default_unknown_estimate_s) > window_start` (the job would still be running when it starts). `default_unknown_estimate_s` is a config key, default 7200. Restartable jobs without an estimate are exempt from the overlap rule, because they can be unloaded at window start (below).
- When a reservation applies to job `j`:
  - Memory ledger: capacity for `j` = `budget − reserve_remaining`, where `reserve_remaining = max(0, r.memory_bytes − Σ memory_bytes of held jobs of r.project)`.
  - Live check: `live − reserve_remaining ≥ j.memory_bytes`.
  - GPU: `r.gpu == "exclusive"` → `j.gpu != "none"` is blocked (unless a held job of `r.project` is already the exclusive holder, in which case normal GPU rules block anyway). `shared` → counts as one shared slot.
  - `blocked_by` code: `reservation_window:<name>`. `why`: "the <name> window (<start>–<end>) reserves <mem> GiB and the GPU for <project>; this job would overlap it (ends ~<time>)".
- Jobs of `r.project` are unaffected, and they bypass the night-window queue restriction while the window is active (it's their window), unless they're atomic and wouldn't finish before `end`. In that case the fit-window applies against `end` only when `allow_spill` is false. Simplest rule: for owner-project jobs during their active window, treat `queue` as `day`.
- **At window start** (latched per reservation per civil date in `meta`, like the morning edge, as phase 4b): if `unload_restartable`, run the preemption planner for a virtual target of size `r.memory_gib` / `r.gpu` (mode `morning` semantics: ignore `preempt_restartable` and priority, never atomic). Victims are restartable daemon-spawned jobs of other projects. Cooperative jobs of other projects that would block → one checkpoint request (`stop_reason="reservation"`). Atomic jobs → left alone (they should never have started, thanks to the overlap rule, unless their estimate was wrong), with event `reservation_blocked_by_atomic`.
- `status` gets a `reservations` array: name, active, next_start, next_end, reserve_remaining_gib, and owner jobs held. The dashboard shows active/upcoming reservations in the header strip and shades them on the timeline.

Tests: `test_reservation_blocks_other_project_gpu_job_during_window`, `test_reservation_allows_cpu_job_that_fits_remaining`, `test_reservation_overlap_rule_blocks_long_job_before_window`, `test_reservation_short_job_before_window_runs`, `test_reservation_owner_project_unaffected_and_day_queue_treatment`, `test_reservation_wraps_midnight_days_rule`, `test_reservation_start_unloads_other_restartable_once_per_day`, `test_reservation_never_signals_atomic`, `test_reservation_dst`, `test_reservation_blocked_by_text`, `test_status_lists_reservations`.

## 3. Hardening from the review

1. **Tick exception guard.** Wrap each phase executor in `try/except Exception`. On an exception: log a traceback, emit event `tick_error` with the phase name, **skip admission for this tick** (fail closed), and continue the loop. If the same phase fails on 20 consecutive ticks, emit `tick_error_persistent` (status warning). The daemon must not exit because of a policy or bookkeeping bug. SQLite `OperationalError` (disk full, locked) is treated the same way. The RPC dispatcher already isolates handler errors; verify it does.
2. **Job log cap.** Config `job_log_max_bytes` (default 1 GiB) and `job_log_keep_bytes` (default 64 MiB). Each full tick (at most every 10 s per job), if `stdout.log` or `stderr.log` of a held daemon-spawned job exceeds the max: copy the last `keep` bytes to `<name>.1` (overwrite), then `os.truncate(path, 0)`. The child's O_APPEND fd continues at the new end. Emit `job_log_truncated`. Doc that external jobs' logs aren't managed.
3. **Sleep and clock-jump safe timers.** Keep in memory, per tick, the deltas `(wall_ms, mono)`. If wall time advanced more than monotonic time by more than 30 s between ticks (system sleep, or an NTP jump forward), shift `stop_requested_ms` of every `held` job in `stopping` or `checkpointing` forward by the skew. Then grace and kill timers don't fire immediately after wake. Emit `clock_skew` with the amount. A backward jump of more than 30 s: shift them backward by the skew (clamped so they never land in the future beyond now). `limit_s` keeps wall semantics (documented: it includes sleep time).
4. **`lsof` off the tick.** Run `port_owner_pids` via a non-blocking `subprocess.Popen` whose stdout is registered in the selector, and attach the pid to the offender row when the result arrives. The tick never blocks on it.

## 4. Install option

`gsched install --venv-symlinks`: create the venv with symlinks (the default `--copies` stays), so the daemon runs as the **same interpreter binary** given by `--python`. Use this when that interpreter already has Full Disk Access (or works from LaunchAgents under ~/Documents). `doctor` then prints the realpath to check.

Tests: `test_install_venv_symlinks_flag`.

## 5. TCC guard (urgent addendum, found live)

A launchd-started process that opens or `chdir`s into `~/Documents`, `~/Desktop`, `~/Downloads` (or iCloud Drive) without TCC approval does not get EPERM: it **blocks** in `open()`/`chdir()` waiting for a consent prompt (SIGKILL still works). In the one-thread daemon that freezes everything. Rules:

1. The daemon process never touches a path inside a protected root (defaults above plus `tcc_protected_roots`). `gsched/tcc.py` classifies paths lexically, following symlinks with `readlink` only outside the roots, and keeps a per-root access state (`ok`, `blocked`, `denied`, `absent`, `unknown`) filled by a probe subprocess polled through the selector (`platform.spawn_async`, killed through `platform.kill_async` after 3 s). Probed at startup, every 600 s, and on demand. Submit-time behavior: 03 §5 steps 4b, 6, 7.
2. Spawning never passes the job's cwd to `Popen`; the shim's workload does the `chdir` after the gate and reports `cwd_ok`; the daemon waits for it without blocking and kills the job with `end_reason=cwd_blocked` on timeout (07 §2).
3. `doctor` (check 7, 8, 18) uses the same cache and never lists a protected folder in-process (11 §4).
4. Other daemon-side uses of user-supplied paths: stdout/stderr paths in a protected root are refused at submit; a profile's `health_bearer_file` inside a protected root is not opened; profile `cwd` goes through the same `check_cwd`; `after` is ids only; project and profile files live under `$GSCHED_HOME`.

Tests (`tests/daemon/test_tcc.py`, using a hook that fails the test if the daemon touches a path under a protected root, the fake-level stand-in for a hang): `test_probe_runs_at_startup_for_every_root_and_never_in_process`, `test_states_ok_denied_blocked_absent`, `test_reprobe_every_600s`, `test_cwd_under_ok_root_is_accepted_without_touching_it`, `test_cwd_under_blocked_root_refused_fast_with_prompt_hint`, `test_cwd_under_denied_root_refused`, `test_unknown_state_starts_probe_replies_retry_and_does_not_block`, `test_unknown_with_no_probe_running_starts_one_on_demand`, `test_symlink_into_protected_root_is_not_followed_into_it`, `test_extra_roots_from_config`, `test_command_under_protected_root_is_not_stat_ed`, `test_path_entries_in_protected_root_are_skipped_not_searched`, `test_log_paths_in_protected_root_are_invalid`, `test_external_job_cwd_in_protected_root_is_recorded_not_checked`, `test_profile_ensure_cwd_uses_the_guard`, `test_spawn_hands_cwd_to_the_shim_not_to_popen`, `test_blocked_chdir_is_killed_and_fails_with_cwd_blocked`, `test_cwd_wait_closed_when_job_ends_first`, `test_doctor_uses_the_cache_and_never_lists`; live: `test_live_shim_chdirs_after_gate_and_reports_cwd_ok`, `test_live_shim_bad_cwd_exits_126_without_running`, `test_live_async_helpers_probe_kill_and_lsof`.

## 6. Judgment calls

Where this document was silent or ambiguous, the choices are recorded in `QUESTIONS.md` (entries 23 onwards).
