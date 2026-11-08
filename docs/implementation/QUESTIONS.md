# Questions and judgment calls

Where the docs were ambiguous, contradicted each other, or described something the platform does not support, this file
records what was chosen (always the most conservative option: fail closed, never signal). Each entry says what the docs
said and what the code does.

## Platform facts that differed from the docs

1. **`os.waitid` does not exist in macOS CPython.** 07 section 3 has the shim call `os.waitid(P_PID, pid, WEXITED|WNOWAIT)` to read
   the zombie's `ri_lifetime_max_phys_footprint` before reaping. On macOS this attribute is missing, so the shim instead polls
   `proc_pid_rusage` on the workload every 0.25 s while waiting (a `SIGCHLD` wake-up pipe makes exit detection immediate) and
   records the largest lifetime-max it saw. The daemon still tracks the per-tick observed peak and `finish` keeps the max of
   both. `test_live_peak_footprint_recorded` prints which value the shim recorded.
2. **`SIGKILL` to the job's process group also kills the shim**, because the shim is in the group. Then no `exit.json` is written.
   06 section 11 specifies SIGKILL to the group. So when a job is `stopping`, our SIGKILL was sent (`kill_sent = 1`) and no record
   exists, `finalize` synthesizes a "killed by signal 9" outcome instead of marking the job `lost`. The state then follows the
   normal mapping (`cancelled` if `cancel_requested`, `failed/signal` otherwise, `limit`/`ceiling`/`abort` as `end_reason`).
3. **`gpu: none` jobs and `tests`**: the live tests use `gpu: none` so that a foreign `llama-server` on the developer's machine
   (protective mode) cannot make them wait.

## Contradictions and gaps in the docs

4. **The key-file pattern in 11 section 6.** The no-personal-data scan is told to flag a particular key-file name, but that very sentence in
   `docs/implementation/11-install-and-doctor.md` contains it. `test_tree_has_no_personal_data` skips lines under
   `docs/implementation/` that mention the test by name; everything else is scanned.
5. **Policy purity vs. the system time zone.** 01/06 say `cfg.timezone == null` means the system zone, and `policy/` must not do
   I/O. `Daemon` resolves an unset timezone once at config load (`config.system_timezone_name`) into the effective config, and
   `policy/windows.py` only ever uses `cfg.timezone` (UTC if somehow unset).
6. **Admission-only ticks.** 05 says they run "phases 0, 1, and 7" with the last sample. Implemented as: reap and finalize exits
   (no signals), rebuild the snapshot from the last memory sample and scan, run phase 7 and the dependency cascade. No scan,
   no maintenance, no signals. If no sample exists yet, a full tick runs.
7. **Observe mode and running jobs.** With `enforce: false`, phases 3-7 are skipped entirely, so a job that was started while
   enforcing and is still running gets no limit/preemption handling until enforcement is back. Exits are still finalized.
   This is the reading of "observe mode changes nothing".
8. **`requeue_restartable` and `attempt`.** 04 does not say whether the attempt number changes. It increments (a new attempt,
   `resume = 0`) and clears `switch_to`, so a switched-away profile is not immediately unloaded again.
9. **`restartable` stop reasons that requeue.** `preempt`, `morning`, `pressure`, `protective`, `switch` requeue; `idle` and
   `drain` end the resident row as `done` (`idle_unload` / `drain_unload`); `limit`, `ceiling`, `abort`,
   `footprint_unreadable`, `cancel`, `terminate` finish the row (`failed` with that `end_reason`, or `cancelled`).
10. **Spawn-abort loop guard.** A job that is aborted before exec (report timeout, shim crash) is requeued unchanged, as
    specified, but after 3 consecutive aborts the job is failed with `end_reason: spawn_error`, so a persistently broken shim
    cannot hot-loop.
11. **Job footprint excludes the shim process** (a ~20 MB Python) but includes every other member of the tree.
12. **External jobs: identity ignores the path.** The launcher's gated child is a bootstrap that `exec`s the real command after
    `attach`, so its path changes with no daemon-visible event. Start time, pid and (for scan) the ppid chain identify it.
    External jobs' trees are the pid plus its descendants, never a process-group sweep (the group may be the launcher's own).
13. **`finish` RPC** requires an attached job (`bad_state` otherwise); `release` is the call for "never attached".
14. **Reconcile for unstarted rows.** A `held` row with `gate_released = 0` whose shim and workload are both gone and that has no
    `exit.json` is requeued unstarted instead of marked `lost`: nothing ever ran, so it is safe to retry.
    *Review (2026-11-08):* only when `pid IS NULL` (the report never arrived, so the gate cannot have been written).
    With a pid recorded the old daemon may have written the gate byte and died before `mark_gate_released` committed,
    so the command may have run; that row is `lost`, never re-run.
15. **`uninstall` flags.** 11 section 3 mentions `--keep-data`, `--force` and `--purge` without a full matrix. Implemented: always
    boot out and remove the plist(s); unless `--keep-data`, also delete `venv/`, `app/`, `bin/` (refused while jobs run, unless
    `--force`); `--purge` additionally deletes state, logs, jobs and checkpoints after printing their sizes.
16. **`install --plist-dir`** (and `GSCHED_LAUNCHAGENTS_DIR`) were added so tests and trial installs never write into the real
    `~/Library/LaunchAgents`. The default is unchanged.
17. **RPC additions.** `events` also accepts `exclude_kinds` and `newest_first` (the dashboard needs "all kinds except sample,
    newest first"); `list` returns every state when `state` is omitted (the CLI filters to non-terminal by default); a `gc` RPC
    backs `gsched gc`; `door: "dashboard"` is accepted and limited to `status`, `list`, `get`, `logs`, `events`, `ping`.
    `status` additionally returns `now_ms`, `queue_counts`, `queued` and `profiles`; job views add `*_ms` timestamps next to
    the ISO ones.
18. **Dashboard prints the token URL only when stdout is a TTY**, so running it under launchd never writes the token into a log.
19. **`cwd` for external jobs** is optional at submit; the CLI fills it with the caller's directory anyway.
20. **MCP argument validation.** Arguments that fail a tool's `inputSchema` are JSON-RPC `-32602` errors (as 10 section 4 says), even
    for `submit_job`, where the daemon's own validation would have produced richer text for an agent to self-correct. Values
    that pass the schema but are rejected by the daemon (`forbidden`, `too_big`, ...) are tool errors.
21. **Test names.** Where a doc test name and the section text disagreed the section text won; for example the "one admit per
    tick" scenario is tested both as a pure policy test and as `test_one_admit_per_tick_then_hint_admits_next` against the
    daemon.
22. **CI.** `.github/workflows/ci.yml` could not be run from this development machine; the macOS and Ubuntu jobs are untested
    until the repository is pushed.

## PR 12 (16-exec-reservations-hardening.md and the TCC addendum)

23. **Reservation defaults.** 16 §2 shows `gpu` and `unload_restartable` in the example but does not say whether they are
    required. `name`, `days`, `start`, `end`, `project`, `memory_gib` are required; `gpu` defaults to `none` and
    `unload_restartable` to `false` (the conservative choice: nothing is held back or signaled that the owner did not
    ask for). `days` accepts `"daily"` anywhere in the list; `start == end` is a 24 h window.
24. **Window-start planner (phase 4b).** 16 says "a virtual target of size `r.memory_gib` / `r.gpu`, mode morning
    semantics". Implemented as a new `window` victim mode in `policy/preempt.py`:
    - the virtual target is what the owner still needs: `reserve_remaining` (not the full `memory_gib`), with `gpu: none`
      when an owner job already holds a GPU slot, and the whole step is skipped when nothing is left to free;
    - victims are restartable and cooperative **daemon-spawned jobs of other projects**; priority and
      `preempt_restartable` are ignored as in morning mode, and **`restartable_min_hold_s` / `coop_min_hold_s` are
      ignored too**: the edge fires once per day, so a victim younger than the hold would otherwise never be asked.
      A resident's `hold` is still honoured;
    - restartable victims get `stop(reservation)` (already in the requeue set), cooperative ones one checkpoint request with
      `stop_reason="reservation"`; both are gated on `unload_restartable`, as is the `reservation_blocked_by_atomic` event;
    - the latch is `meta` key `reservation_latch:<name>` = civil date the window *started* on (a midnight-wrapping window
      is one edge). A daemon that starts mid-window takes the edge on its first full tick, like the morning edge.
25. **Reservation in `fits`.** `reservation_window:<name>` is added only when the plain memory-ledger / live / GPU check
    passed and the reservation-adjusted one fails (otherwise the plain code already explains the block). It is never a
    starvation-reservation reason (like `pressure`: waiting for jobs to finish does not free it). For an owner-project
    job during its active window `queue` is treated as `day` (so neither the night-window restriction nor the
    atomic fit-window applies); `day_waiting_ids` still uses the stored queue. GPU modes: `exclusive` blocks every
    GPU job unless an owner job already holds the exclusive GPU; `shared` reserves one slot unless an owner job already
    holds any GPU slot.
26. **`gsched exec` details.** (a) The argv is also sent as `command` so `list`/`get` show what the job is. (b) An
    atomic/cooperative command without `--est` would sit in `needs_estimate` forever (a wrapper has no human), so
    `default_unknown_estimate_s` is sent as its estimate. (c) A job that was cancelled by someone else while the wrapper
    waits (no lease arrives) exits 2 without running the command: an explicit cancel is not a scheduler fault.
    (d) If the daemon disappears while the job is queued, the wrapper re-parks for up to 30 s, then falls back (or exits 75).
    (e) A dead-daemon, observe or request-error fallback `execvp`s; if `execvp` itself fails the exit code is 126/127.
    (f) Signals that were ignored at start (a `nohup`'d SIGHUP) are not hooked. (g) `--on-max-wait` defaults to `run`
    with fail-open and `fail` with `--fail-closed`. (h) Missing `--mem`/`--checkpoint`/project is a usage error (64) even
    though the wrapper is fail-open: that is a configuration mistake visible on the first run, not a scheduler fault.
    (i) The gated child's bootstrap now reports `exec failed` and exits 127/126 like a shell instead of a traceback, and
    runs with `-I`.
27. **`cancel` gained `end_reason`** (only `max_wait`), so the daemon can log `exec_max_wait` from the cancel as 16 §1 asks.
    It applies to queued jobs and to an external job still in `await_attach`.
28. **External `lost` needs two ticks.** `gsched exec` calls `finish` a few milliseconds after its child exits; a tick in
    that window marked a clean exit `lost` (seen on a real daemon with a 12 ms command). The pid must now be seen gone on
    two ticks at least 0.5 s apart before an external job is `lost`; `finish` in between wins.
29. **Tick guard.** `tick_error` is written for the first 3 consecutive failures of a phase and then every 10th (a
    persistent bug would otherwise write a row per second); `tick_error_persistent` every 20th. Admission-only ticks stay
    closed until a full tick succeeds. If a planner phase fails, later planners do not run on the stale snapshot. A last-resort
    `try/except` in `serve_forever` (`loop_error`) covers selector callbacks outside the tick. The RPC dispatcher already
    isolated handler errors (verified by `test_rpc_handler_exception_is_isolated`).
30. **Job log cap.** Only logs inside the job directory are capped; a user-chosen `stdout`/`stderr` path is never truncated.
    The tail is copied atomically (`.1.tmp` then rename); bytes appended between the copy and the truncate are lost.
31. **Clock skew.** Compared between consecutive *full* ticks; `|wall delta - monotonic delta| > 30 s` shifts
    `stop_requested_ms` by the skew, clamped to `now`.
32. **lsof off the tick** needed a platform abstraction: `Platform.spawn_async(argv)` / `port_owner_start(port)` return an
    `AsyncProc` (selector-registrable fd, non-blocking `poll`, `kill_async` for a helper we started). The scanner kills a
    lookup after 3 s. The synchronous `port_owner_pids` stays on the platform for callers outside the tick. The fake
    finishes immediately by default (`port_lookup_delay_s` simulates a slow one), so `test_foreign_port_listener_is_heavy`
    is unchanged. `Platform.reap()` now records the real exit code for helper Popens it reaps (it used to store 0).
33. **TCC guard.** (a) Classification is lexical plus `readlink` outside the roots; a symlink *inside* a root is never
    followed. (b) A root that does not exist is `absent` and gets the normal checks. (c) With state `ok` nothing under the
    root is stat'ed: a missing cwd or command surfaces as the job's own error. (d) PATH entries inside a protected root are
    not searched; if nothing is found elsewhere and such an entry was `ok`, the command is assumed to resolve there.
    (e) `spawn: external` cwd inside a root is recorded unchecked (the daemon never uses it). (f) The probe's stderr is
    merged into its output to tell EPERM (`denied`) from a timeout (`blocked`). (g) `Daemon(tcc_home=...)` exists so tests
    and the live suite probe a fake home, never the real `~/Documents` (a probe there could raise a real consent prompt).
    (h) `cwd_blocked` stops the job even if it is `atomic`/`cooperative`: nothing of the user's command has exec'd (the
    explicit exception to ground rule 2's policy-stop ban, same reasoning as the spawn-abort SIGKILL). (i) A `chdir` that
    fails (not hangs) now ends the job with exit 126 and a stderr line instead of `spawn_error`. (j) The cwd wait is in
    memory: after a daemon restart a job whose chdir hangs is not watched. (k) `test_doctor_reports_tcc_warning` asserted
    that the doctor calls `listdir` on `~/Documents`; that is exactly what the addendum forbids, so it was rewritten to
    drive the probe cache and assert that nothing under the protected roots is listed.
34. **Status/dashboard.** `reservations[].next_start/next_end` describe the active window while one is active. The timeline
    shading data (`windows`) is only returned to the dashboard door to keep the MCP `get_status` payload small.
35. **Signal helpers for the wrapper** (`signal_own_child`, `exit_by_signal`) live in `platform/darwin.py` because ground
    rule 1 allows signal sends only there; they act only on the wrapper's own direct child or on the wrapper itself.
36. **`install --venv-symlinks`** rebuilds an existing venv when the requested mode differs from what is on disk.


## 37. `gsched exec` child leads its own process group (orchestrator, live test 2026-11-08)
Sharing the wrapper's process group made group-wide signals (terminal Ctrl-C, a supervisor killing the group) reach the child twice: once directly and once via forwarding. llama-server treats a second SIGTERM as "terminate immediately" and hit `GGML_ASSERT([rsets->data count] == 0)` in Metal teardown (SIGABRT). Now the child is `process_group=0`, and the wrapper forwards exactly once. Residual: SIGKILL of the wrapper orphans the child (it continues; the scan sees a foreign process).

## 38. Job memory = max(phys_footprint, resident) for our own job trees (orchestrator, live test 2026-11-08)
llama.cpp mmaps GGUF weights. Live: footprint 8.4 GiB, resident 26.1 GiB, GPU in-use 27.2 GiB, and reclaimable fell about 20 GiB. Footprint alone made `pending = reservation - footprint` about 18 GiB forever, which double-subtracted memory already gone from the reclaimable figure. Foreign/noted classification stays footprint-only (RSS includes shared maps).

## 39. `gsched exec --connect-grace` (default 20 s) (orchestrator, live test 2026-11-08)
A wrapper that started while the daemon was restarting failed open immediately and ran llama-server unscheduled. Protective mode caught it, but the slot accounting was lost. A missing or refused socket (`ConnectError`) is now retried every 0.5 s for the grace period before failing open. Protocol errors still fail open immediately.

## 40. `gsched exec --replace` (in-place mode) (orchestrator, live test 2026-11-08)
Wrapping a LaunchAgent so that the command became a *child* of the gsched Python changed which binary macOS TCC treats as responsible for the process. llama-server loads dylibs from `~/Documents/...` through its LC_RPATH, and a T233 job's venv lives in ~/Documents. Under launchd, such opens hang on a TCC prompt instead of failing. `--replace` waits for admission, attaches the wrapper's **own** pid, and `exec`s the command, so pid, ancestry, TCC attribution, and launchd's SIGTERM delivery are identical to running without the wrapper. The daemon recognizes an in-place attach (attached pid == the caller's peer pid), stores `meta in_place:<job>`, and when the pid goes it ends the job as `done` / `exit_unobserved` instead of `lost`. This is the recommended mode for LaunchAgents. The child mode stays the default for scripts that need the exit code reported.

## 41. `yield_apps`: quit an owner-named desktop app while its memory is needed (orchestrator, owner request 2026-11-08)
The owner asked for a data terminal (about 2 GiB across 15 Electron processes, needed for data ingest) to be closed automatically when heavy work needs memory and reopened afterwards. This is the only path that signals a process the daemon did not start, so it is opt-in per bundle and narrow. Triggers: a named reservation window is active (episode `r:<name>:<date>`), or a queued job of at least `for_jobs_gib` has been blocked only by `memory_live` for `job_wait_s` and `live + 0.8 * app footprint` would fit it (episode `j:<id>`; it lasts while that job is queued or running, and while any heavy job admitted after the closure still runs). Safety: `busy_ports` postpones the quit while another process holds an ESTABLISHED connection to the app's local API (`lsof`, an async helper, retried every 30 s); the quit is AppleScript `quit` or one SIGTERM to the top-level main executable (Electron/Chromium shut down gracefully on SIGTERM; plain Cocoa apps do not, hence the AppleScript default); never SIGKILL; after `quit_timeout_s` the episode is skipped. Only an app the daemon closed is reopened (`open -g -a`, `reopen_delay_s` after the need ends); if the owner reopens it mid-episode, that episode never closes it again. State persists in `meta yield_app:<name>`, so an app closed before a daemon restart is still reopened, and an app whose entry leaves the config is reopened at once. A yield_apps exception is logged as `tick_error` but does not freeze admission. Events: `yield_app_quit`, `_closed`, `_busy`, `_quit_refused`, `_quit_failed`, `_kept_closed`, `_reopened_by_user`, `_reopened`, `_reopen_failed`, `_released`, `_unconfigured`. Live-tested on Calculator (`tests/live/test_yield_live.py`).
