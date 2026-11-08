# Changelog

## Unreleased

- `yield_apps` (opt-in): quit a named desktop app gracefully while a reservation window or a heavy memory-blocked job
  needs its memory, and reopen it in the background afterwards. Respects `busy_ports`, never force-kills, never reopens
  an app it did not close. Shown in `status` and the dashboard.

## PR 12

- `gsched exec`: fail-open wrapper that runs any existing command as an external job (exact exit status, same process
  group, `--max-wait`, `--fail-closed`).
- Time `reservations` (weekly windows holding memory and the GPU for one project), shown in `status` and the dashboard.
- Hardening: tick phases are exception-guarded (`tick_error`), per-job log cap (`job_log_max_bytes`), sleep and
  clock-jump safe grace/kill timers (`clock_skew`), `lsof` runs off the tick.
- TCC: the daemon never touches a path under `~/Documents`, `~/Desktop`, `~/Downloads` or iCloud Drive (a launchd process
  blocks there behind a consent prompt). A probe subprocess caches access per root; jobs `chdir` in the workload, not
  the daemon (`end_reason: cwd_blocked`); stdout/stderr paths in protected roots are refused.
- `gsched install --venv-symlinks`.
- New config keys: `reservations`, `default_unknown_estimate_s`, `job_log_max_bytes`, `job_log_keep_bytes`,
  `tcc_protected_roots`.

## 0.1.0 (beta)

First release. Observe mode is the default: `gsched install` writes `enforce: false`, so nothing is scheduled, spawned
or signaled until you run `gsched config set enforce true`.

- Daemon (`gsched daemon`): single-threaded event loop, SQLite state, Unix socket RPC with peer-credential checks.
- Memory-packing admission against a ledger budget and the live memory sample, with GPU modes
  (`exclusive`, `shared`, `none`), CPU cores, named locks, and a starvation reservation.
- Day and night queues with a fit-window for atomic jobs and a once-per-day morning edge.
- Three checkpoint modes (`restartable`, `cooperative`, `atomic`); atomic and spilled jobs are never signaled on policy.
- Per-job shim: exit status, logs and process identity survive daemon restarts; release gate before exec.
- Protective mode for foreign heavy processes (never signaled), memory-pressure ladder, history-based `suggest`.
- Resident profiles (`ensure`, `inhibit`, `hold`, idle unload, same-project switch).
- CLI (`gsched run`, `wait`, `logs --follow`, `status --brief`, ...), MCP stdio adapter (`gsched mcp`), `AGENTS.md`.
- `gsched install` (private venv + LaunchAgent) and `gsched doctor` (including the TCC / Full Disk Access check).
- Read-only, loopback-only, token-protected web dashboard (`gsched dashboard`), a separate opt-in process.
