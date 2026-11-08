# Design review: DESIGN.md v1 (2026-11-07) → v2 (2026-11-08)

The v1 draft is archived privately at `docs/local/DESIGN-original-2026-11-07.md`. The repo isn't under git yet, so that copy is the only history. This document records what was kept, what was wrong or missing, and what changed.

Before any of this goes into the repo, see **Finding R-1**: the v1 file contained personal paths, a tailnet IP address, and LaunchAgent labels in the repo root.

## Verdict

v1 is careful and correct about the hard safety properties: no fake checkpoints, no signals to foreign processes, commit before reply, identity-checked signals, observe mode, and no LLM in the loop. Those survive unchanged in v2.

It falls short of the stated goal ("make full efficient use of my resources", "accommodate a full range of ideas", "usable by other Mac Studio owners") in three ways:

1. **One heavy job at a time, globally.** A 128–512 GB Mac Studio would sit mostly idle, and CPU-only work had no proper place (the "light ≤ 2 GiB" class was the only side channel).
2. **Supervision didn't survive a daemon restart.** Exit codes, logs, and wrapper-script footprints were not handled.
3. **It was over-specified around one machine's history.** Line numbers in private repos, byte-exact bootstrap strings, and a tick function with roughly 20 ordered branches made it hard to implement and harder for outsiders to understand.

v2 keeps the safety core, generalizes admission to memory packing, adds the supervision and agent-facing pieces that were missing, and moves machine-specific material out of the public design.

## What was kept as is

- Admission instead of time-slicing. `atomic` / `cooperative` / `restartable` and their signal rules. `on_timeout: wait` as the default. `kill` is opt-in and refused for atomic.
- One versioned spec for all doors. MCP as a thin proxy. No wait tool that blocks for hours.
- A single-threaded daemon. `commit_admit` commits before any response or spawn. Socket close after `submit` doesn't cancel. A parked `wait --until start` is the only lease handoff.
- The release gate: the child blocks on a pipe until its identity is committed.
- Identity check (pid + start time + path) before every signal. ctypes layout failure exits 78.
- Day/night queues, `queue: auto` with `day_max_s`, the civil-time night window, the atomic fit-window, `allow_spill`, the morning edge latched once per civil date, and `needs_estimate`.
- Foreign heavy processes trigger protective mode and are never signaled. IDE and browser footprint is recorded only.
- `preempt_restartable: false` until a rehearsal has been done.
- Projects as files. Profiles as data. `ensure` / `inhibit` / `hold`.
- Observe mode as the install default.
- Stdlib-only Python 3.12+. Copy-not-symlink install. Refusing Python below 3.12. Apache-2.0.

## Findings

Severity: **H** breaks a goal or loses data, **M** wrong or surprising behavior, **L** polish.

### Privacy and publishability

| # | Sev | Finding | Change |
|---|---|---|---|
| R-1 | H | `DESIGN.md` sits at the repo root and its appendix and references contain home-directory paths, a Tailscale IP, personal reverse-DNS LaunchAgent labels, an API-key filename, and private repo internals. v1 itself said "the appendix does not ship in the repository", but the file is in the repository | Appendix moved to `docs/local/this-machine.md`. `docs/local/` is gitignored. The public design has no personal data. `test_tree_has_no_personal_data` also scans `DESIGN.md` and `docs/` (except `docs/local/`) |
| R-2 | M | The core `BOOTSTRAP` string had to be "the same bytes as `execution.py` line 484" in a private repo. That couples the public core to private code for no functional reason: the daemon never needs to match an external launcher's bootstrap | Removed. The core ships its own shim and gate. External launchers use any gate they like and the `attach` / `finish` RPCs |
| R-3 | L | References section pointed at private files and line numbers | Moved to `docs/local/` |

### Resource efficiency

| # | Sev | Finding | Change |
|---|---|---|---|
| E-1 | H | Global single heavy slot. On a 192 GB Studio, a 20 GiB experiment blocks every other experiment even when 150 GB is free. Contradicts "full efficient use" | Memory ledger + live gate. Multiple jobs run at once when they fit. The GPU is arbitrated separately: `exclusive` (one job, the default) / `shared` / `none`. The v1 guarantee holds for `exclusive` GPU work |
| E-2 | H | No CPU jobs as a first-class concept. "light" meant ≤ 2 GiB, skipped fairness, and could not express "a 30 GiB CPU data-prep job while the GPU trains" | `gpu: none` + `cpu_cores`. Every job goes through the same admission and fairness. The light path is removed |
| E-3 | H | Admission gate read only the instantaneous `reclaimable`. Once more than one job can run, a job that was just admitted hasn't allocated yet, so the next tick sees the memory as free and over-admits. (v1 already had this exposure between a heavy job and lights) | `pending` = reserved bytes not yet touched is subtracted from the live sample. A ledger budget (`memsize − os_reserve`) is a second, independent check |
| E-4 | M | No "manage my RAM" behavior beyond per-profile abort lines. Under real pressure the only actor was the OS (jetsam) | Pressure ladder: `warn` stops admits; `critical` unloads our restartables, optionally checkpoints cooperatives, never touches atomic |
| E-5 | M | Job footprint was the holder pid only. A resident started through `start-llama.sh` reads as a few MB while `llama-server` (its child) holds 27 GB. A descendant `llama-server` could also be misclassified as foreign | Footprint is summed over the job's process group and ppid-descendants. Anything in that tree is `ours` |
| E-6 | M | Nothing learned from history. Agents guess `need_gib` | `history` table (peak footprint via `ri_lifetime_max_phys_footprint`, runtime). `gsched suggest`. Submit returns `warnings` when the request is below the historical p95 |
| E-7 | L | Night jobs and a sleeping Mac: nothing kept the machine awake for overnight runs | `caffeinate -i -w <shim pid>` per running job (`prevent_sleep`) |

### Correctness and robustness

| # | Sev | Finding | Change |
|---|---|---|---|
| C-1 | H | **TCC.** v1 noted that LaunchAgents can't read `~/Documents` and so moved the *install*, but jobs spawned by the daemon inherit the same restriction. Your own repos (`~/Documents/latent-alpha`, `~/Documents/local-llm`) would fail with `EPERM` the first time the daemon spawned them | Submit checks the job's `cwd` from inside the daemon and refuses with `cwd_not_accessible` plus a fix. `gsched doctor` tests Documents/Desktop/Downloads access and prints the exact interpreter path to grant Full Disk Access. The install uses a stable venv interpreter path so the grant survives Homebrew upgrades |
| C-2 | H | **Exit status lost across daemon restarts.** Children were plain `Popen` children of the daemon. After a crash or `KeepAlive` restart they're reparented to launchd, and the new daemon can't `waitpid` them. Cooperative success requires "exit 0 + READY", so a restart during a checkpoint loses the continuation | A per-job shim waits for the workload and writes `exit.json` atomically. Startup reconciliation finalizes rows from those records |
| C-3 | H | **Job stdout/stderr unspecified.** Agents can't debug a job they can't read | `$GSCHED_HOME/jobs/<id>/{stdout,stderr}.log`, `gsched logs`, MCP `get_job_logs` |
| C-4 | M | "Clean environment plus spec env": with no `PATH` or `HOME`, `python3` or `bash` won't resolve and many tools break | A defined minimal base env (`PATH` from config, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `TMPDIR`) + spec `env` + `GSCHED_*` |
| C-5 | M | `cwd: null` was undefined, and so were job id format, the stdin source, and the umask | CLI fills `cwd`. MCP requires it or uses the project's `default_cwd`. stdin is `/dev/null`. umask 077 for daemon files, the job's own umask is 022. Ids are `YYYYMMDDTHHMMSS-xxxx` |
| C-6 | M | MCP version negotiation was wrong. v1 said to error if the client didn't offer `2025-11-25`. The MCP lifecycle spec says the server responds with the requested version if it supports it, and otherwise with a version it does support, letting the client decide | Supported set `2025-11-25`, `2025-06-18`, `2025-03-26`. Echo if supported, else answer with the latest |
| C-7 | M | Platform check used `uname -m`. Under Rosetta an x86_64 Python reports `x86_64` on Apple silicon, and the reverse case isn't covered | `sysctl hw.optional.arm64 == 1` **and** `platform.machine() == "arm64"`. Refuse Rosetta with a clear message |
| C-8 | M | Memory probes ran `vm_stat` / `sysctl` subprocesses every 1 s tick | `host_statistics64` and `sysctlbyname` through ctypes. Verified on this machine (macOS 27.0, M5 Max): offsets free@0, purgeable@88, speculative@92, external@136 match `vm_stat`. Note: the SDK's `vm_statistics64` is now 416 bytes / count 104 and the kernel returned count 40, so **do not assert a fixed struct size**; pass a large buffer and require count ≥ 38 |
| C-9 | M | Listen-port detection via `PROC_PIDFDSOCKETINFO` on every process every second: large, version-sensitive structs and an expensive scan | A non-blocking TCP connect probe to `127.0.0.1` / `::1` on configured ports every 5 s, with ownership by matching our jobs' `listen_port`. Pid attribution (for the offender record only) is a one-off `lsof -t` when a probe flips |
| C-10 | M | `getpeereid` named, but Python has no binding for it | ctypes `libc.getpeereid(fd, &uid, &gid)` (verified), plus `getsockopt(0, 2)` for `LOCAL_PEERPID` (verified) |
| C-11 | M | Signals to the holder pid only. A restartable started via a shell script leaves its real server alive after `SIGTERM` to the shell | Termination signals go to the process group (`killpg`). `SIGUSR1` goes to the workload pid only, which is the documented cooperative contract |
| C-12 | M | `tick()` ran after every select wakeup, not once per second, so a burst of requests caused a burst of full process scans | The tick runs when ≥ 1 s has passed (monotonic clock). Request handlers may trigger a cheap "admission-only" pass |
| C-13 | L | Socket path length (104-byte `sun_path`) not checked. A long `GSCHED_HOME` fails mysteriously, and pytest temp dirs under `/var/folders` exceed it | Checked at startup (exit 78 with a message). `socket_path` is overridable. Tests use short `/tmp/gs-XXXX` paths |
| C-14 | L | Partial reads on non-blocking sockets weren't addressed (`read_one_json_nowait`) | Per-connection buffers, a 1 MiB limit, and a 10 s idle timeout for non-parked connections |
| C-15 | L | Two unit systems (GiB start gates, decimal-GB ceilings) in the public spec, plus a page of arithmetic warnings to keep them apart. A trap for every new user | Public API is GiB only. Bytes internally. Decimal-GB constants from private repos are converted in the adapter (noted in `docs/local/this-machine.md`) |

### Agent usability

| # | Sev | Finding | Change |
|---|---|---|---|
| A-1 | M | An agent can't ask "why isn't my job running?" (v1 had `skip` in `status` only) | `get` returns `blocked_by` + `why` |
| A-2 | M | Agents can't check capacity before submitting | MCP `get_status`. CLI `gsched status --brief` |
| A-3 | M | No wait in MCP means agents poll `get_job` repeatedly, and each poll costs tokens | `get_job` takes `wait_s ≤ 30` as a bounded long-poll. Still no hours-long blocking |
| A-4 | M | Experiment pipelines (prep → train → eval) needed external orchestration | `after: [job ids]` dependencies |
| A-5 | M | MCP gave any MCP-only agent arbitrary command execution as the user | Per-project `mcp_submit` opt-in, optional `command_allow` |
| A-6 | M | A user or agent couldn't stop a running atomic job through the tool. "cancel" only set a flag, which confuses agents | `cancel --terminate` for jobs the daemon spawned: an explicit user kill, distinct from policy. Gated for MCP by `mcp_terminate` |
| A-7 | L | Writing JSON specs by hand is friction for humans and agents | `gsched run [flags] -- argv...` |
| A-8 | L | Observe mode returned `would_admit` with no explanation. A new user or agent would think the job ran | `state: observe` + `would` + `hint` |

### Simplification

| # | Sev | Finding | Change |
|---|---|---|---|
| S-1 | M | Deficit round-robin with credit rounds, caps, aging, `segment_charged`, charge points that depend on mode, and "an aging round does not credit". Correct, but it has many interacting rules and was written for a single slot. With concurrency, "credit once per round" stops being well-defined | Decayed weighted fair share (resource-seconds with a half-life). It is pure and needs no rounds or caps: decay bounds lockout, and a reservation bounds starvation. `test_same_priority_alternates` still holds |
| S-2 | M | `tick` was a single function with about 20 ordered early returns, written around one `holder` | `tick` becomes phases over all running jobs: observe → reconcile → per-job maintenance → pressure → preemption plan → admit one. Each phase is a pure planner plus an executor. See `docs/implementation/06-tick.md` |
| S-3 | L | `light` class, `light_max_gib`, `commit_light`, `admit_lights` | Removed (E-2) |
| S-4 | L | `held_reason=await_attach` vs `live` vs spawn rollback states spread across many functions | `held_reason ∈ {spawning, await_attach, live, checkpointing, spilled, stopping}` with one table of owners |

## Things deliberately not added

- **GPU utilization as an admission gate.** `ioreg` gives device utilization, but WindowServer and video playback also use the GPU. It's shown in `status`, with an opt-in rule (`gpu_busy_foreign_pct`) marked experimental.
- **Changing `iogpu.wired_limit_mb`.** That needs `sudo`. `doctor` reports it and explains.
- **Automatic `memory_gib` from history.** The daemon only suggests and warns. A silent change would break the "the job spec is the contract" rule.
- **A TCP or HTTP API.** Still a non-goal for v1.

## Name mapping v1 → v2

| v1 | v2 |
|---|---|
| `memory.need_gib` | `resources.memory_gib` |
| `memory.ceiling_gb` (decimal) | `resources.ceiling_gib` |
| `memory.kind: heavy/light` | `resources.gpu: exclusive/shared/none` + size |
| `memory.level_min` | `resources.level_min` |
| `light_max_gib`, `commit_light` | removed |
| `deficit_ms`, `cap_ms`, `q_ms`, `credit`, `starve_ms` aging | `usage`, `fair_halflife_s`, `starve_s` reservation |
| `footprint_alarm_gb` | `footprint_note_gib` |
| `abort_swap_rise_bytes`, `abort_footprint_ceiling_gb` | `abort_swap_rise_gib`, `abort_ceiling_gib` |
| `refused` state | removed (never reachable in v1 either). Refusals return errors and insert nothing |
| `would_admit` | `state: observe` + `would` |
| `release` (lease) | `release` (no pid attached) + new `finish` (with exit code) |
| `user_version = 1` | `user_version = 2`. No v1 database ever existed, so no migration |

## Owner decisions

Resolved 2026-11-08: the owner kept all three defaults (`backfill: false`, `os_reserve_gib = max(8, 12.5 %)`, night window on). They're user-editable in config. See DESIGN.md §19.
