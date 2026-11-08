# 01: Repo, conventions, config

## 1. Repository layout

```
gpu-scheduler/
  LICENSE                       Apache-2.0 full text
  README.md                     user-facing (outline in 11)
  AGENTS.md                     snippet for agent sessions (text in 10)
  DESIGN.md
  CONTRIBUTING.md               how to run tests, ground rules link
  pyproject.toml
  .gitignore                    includes docs/local/
  gsched/                       (module map in README.md)
  deploy/com.gpuscheduler.agent.plist.template
  examples/
    checkpoint_counter.py       reference cooperative worker
    jobs/atomic.json
    jobs/cooperative.json
    jobs/restartable.json
    jobs/cpu-only.json
    jobs/pipeline-*.json        prep -> train -> eval using "after"
    projects/example-train.json
    profiles/llama-server.example.json
    config/workstation.json     night window on, backfill off (this owner's style)
    config/dedicated.json       night window off: a box that only runs jobs
    adapters/README.md          prose: how a project launcher integrates
  tools/check_layouts.c         dev-only struct layout printer
  tests/
  docs/
    design-review.md
    implementation/
    local/                      gitignored, never published
```

## 2. `pyproject.toml`

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "gpu-scheduler"
version = "0.1.0"
description = "Local admission control for Apple silicon unified memory: memory-packing job scheduler for humans and LLM agents"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.12"
dependencies = []
classifiers = ["Operating System :: MacOS", "Programming Language :: Python :: 3.12"]

[project.optional-dependencies]
mcp = []          # stable install target; the adapter needs nothing extra
dev = ["pytest>=8"]

[project.scripts]
gsched = "gsched.cli:main"

[tool.setuptools.packages.find]
include = ["gsched*"]

[tool.pytest.ini_options]
markers = ["live: needs a real Apple silicon kernel (ctypes probes, real processes)"]
addopts = "-m 'not live'"
```

Run live tests with `pytest -m live`.

## 3. Coding conventions

- Python 3.12, type hints everywhere, `from __future__ import annotations` not needed. Use `dataclasses(frozen=True, slots=True)` for policy models.
- Formatting: whatever `ruff format` would produce (ruff is not a dependency; don't add a config that requires it).
- Time: **integer epoch milliseconds** (`*_ms`) for everything stored, plus `float` monotonic seconds for in-process durations (`*_mono`). Never mix them. Civil time is computed only in `policy/windows.py`.
- Memory: **integer bytes** (`*_bytes`). Conversions live only in `gsched/units.py`:
  ```python
  GiB = 2**30
  def gib_to_bytes(x: float) -> int: return int(round(x * GiB))
  def bytes_to_gib(b: int) -> float: return b / GiB
  ```
- No `print` in daemon code. Use `log.decision(kind, **fields)` (see 12).
- Errors: raise `GschedError(code, message, **detail)` (see §5). Handlers turn it into an RPC error.
- No global mutable state except the `Daemon` object. The `Platform`, `Clock`, `Store`, and `Config` are passed in, so tests can substitute fakes.

## 4. Clock

```python
class Clock(Protocol):
    def now_ms(self) -> int: ...        # wall clock, epoch ms
    def mono(self) -> float: ...        # monotonic seconds

class SystemClock:  # time.time_ns()//1_000_000, time.monotonic()
class FakeClock:
    def __init__(self, start_ms: int): ...
    def advance(self, seconds: float) -> None: ...   # moves both clocks
    def set_wall(self, ms: int) -> None: ...         # simulate NTP/sleep jump
```

Tests never call `time.sleep`. The daemon loop takes a `Clock`. In tests, the loop is driven with `daemon.step()` (one select with timeout 0, then a tick if due) instead of `serve_forever()`.

## 5. Errors

`gsched/errors.py`:

```python
class GschedError(Exception):
    def __init__(self, code: str, message: str, **detail): ...
```

| Code | Meaning | CLI exit |
|---|---|---|
| `schema` | `schema` field missing or not `gpu-scheduler.job.v1`, or the request isn't valid JSON | 4 |
| `invalid` | Field validation failed. `detail.field` names the field | 4 |
| `unknown_project` | No project file with that name | 2 |
| `unknown_job` | No such job id | 2 |
| `unknown_profile` | No such profile | 2 |
| `forbidden` | Door permission (e.g. MCP submit not enabled for the project, `command_allow` mismatch, MCP `terminate` not allowed) | 2 |
| `cwd_not_accessible` | The daemon can't list `cwd` (TCC or permissions). `detail.hint` explains Full Disk Access | 2 |
| `command_not_found` | `command[0]` doesn't resolve on the job's `PATH` | 2 |
| `draining` | The daemon is draining and refuses new submissions | 2 |
| `too_big` | `memory_gib` exceeds `budget_bytes`, so the job could never fit | 2 |
| `bad_state` | The operation isn't valid in the job's current state | 2 |
| `bad_lease` | Lease unknown or doesn't match | 2 |
| `still_running` | `release` while the attached pid is alive | 2 |
| `internal` | Bug. Logged with a traceback. The daemon keeps running | 4 |

## 6. Paths

All paths derive from `GSCHED_HOME` (default `~/.local/gpu-scheduler`, `~` expanded with `os.path.expanduser`, then `realpath`).

| Path | Mode | Purpose |
|---|---|---|
| `$H/config.json` | 0600 | Config |
| `$H/env` | 0600 | Optional `KEY=VALUE` lines used to expand `${VAR}` in profiles |
| `$H/projects/*.json` | 0600 | Project files |
| `$H/profiles/*.json` | 0600 | Resident profiles |
| `$H/state/gsched.sqlite` | 0600 | Database (+ `-wal`, `-shm`) |
| `$H/run/` | 0700 | Runtime dir |
| `$H/run/gsched.sock` | 0600 | Socket (overridable with `socket_path`) |
| `$H/run/daemon.pid` | 0600 | pid + start time, written after listen, removed on clean exit |
| `$H/jobs/<id>/` | 0700 | Per-job dir: `stdout.log`, `stderr.log`, `exit.json`, `spec.json` (no env values) |
| `$H/checkpoints/<id>/` | 0700 | Default `GSCHED_CHECKPOINT_DIR` |
| `$H/logs/gsched.log` | 0600 | Decision log |
| `$H/logs/launchd.out.log`, `launchd.err.log` | 0600 | launchd capture, early output only |
| `$H/venv/` | | Runtime interpreter (created by install) |
| `$H/app/` | | Copied package (created by install) |
| `$H/bin/gsched` | 0755 | Wrapper script |

Set `os.umask(0o077)` in the daemon at startup. Create directories with `os.makedirs(p, mode=0o700, exist_ok=True)`, then `os.chmod`, because `makedirs` mode is masked.

Socket path length check: `len(os.fsencode(path)) < 104`. Otherwise exit 78 with `socket path too long (N bytes); set socket_path in config or a shorter GSCHED_HOME`.

## 7. Config reference

`$H/config.json`. A missing file means all defaults, which includes `enforce: false`. Unknown keys log `config_unknown_key` once and are ignored. Bad values: on **startup**, exit 78 with the key named. On **reload** (mtime change, checked each tick, or the `reload` RPC), keep the old config and log `config_reload_failed`. `Config` is a frozen dataclass. The tick reads `self.cfg` once at its start.

| Key | Type | Default | Validation / notes |
|---|---|---|---|
| `enforce` | bool | `false` | Observe mode when false |
| `timezone` | str\|null | `null` | IANA name. null = system local (`/etc/localtime` via `zoneinfo`; fall back to `time.tzname` → UTC with a warning) |
| `night_window` | obj\|null | `{"start":"23:00","end":"08:00"}` | `HH:MM`, start ≠ end. `null` disables queues: everything is `day` and `fit_window` never applies |
| `day_max_s` | int | 7200 | > 0. `auto` sends `estimate_s > day_max_s` to night |
| `backfill` | bool | `false` | Night jobs may run outside the window when no day job is waiting |
| `morning_unload_restartable` | bool | `true` | See 06 §7 |
| `allow_spill` | bool | `false` | Default for jobs that omit it |
| `on_timeout` | str | `"wait"` | `wait`\|`kill`. Default for jobs that omit it |
| `grace_s` | int | 120 | 1..86400. Default for jobs that omit it |
| `kill_grace_s` | int | 15 | 1..300. SIGTERM → SIGKILL delay |
| `coop_min_hold_s` | int | 300 | |
| `restartable_min_hold_s` | int | 60 | |
| `preempt_restartable` | bool | `false` | Fairness/priority preemption of restartables. The morning edge and the pressure ladder ignore this |
| `preempt_grace_ms` | int | 180000 | Slots grace for residents |
| `fair_halflife_s` | int | 21600 | Usage decay half-life |
| `starve_s` | int | 14400 | Eligible-but-blocked time before a job becomes the reservation |
| `os_reserve_gib` | num\|null | `null` | null = `max(8, 0.125 × memsize_gib)`. Budget = memsize − reserve. Must leave budget > 0 |
| `headroom_gib` | num | 2 | Extra live headroom on every admit |
| `gpu_shared_max` | int | 2 | ≥ 1 |
| `cpu_budget` | int\|null | `null` | null = `hw.physicalcpu` |
| `footprint_note_gib` | num | 2 | `noted` threshold |
| `heavy_basenames` | [str] | `["llama-server","vllm","ollama"]` | Exact basename match on `proc_pidpath` |
| `heavy_listen_ports` | [int] | `[]` | 1..65535 |
| `port_probe_s` | int | 5 | Probe cadence |
| `protective_unload_restartable` | bool | `true` | |
| `protective_clear_s` | int | 30 | |
| `pressure` | obj | see below | |
| `pressure_checkpoint_cooperative` | bool | `false` | |
| `pressure_step_s` | int | 10 | Min seconds between ladder actions |
| `checkpoint_signal` | str | `"SIGUSR1"` | `SIGUSR1`\|`SIGUSR2` only |
| `attach_grace_ms` | int | 30000 | External: lease issued, no attach yet |
| `spawn_report_timeout_ms` | int | 10000 | Shim must report the workload pid within this |
| `prevent_sleep` | bool | `true` | caffeinate per running job |
| `base_path` | str | `"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"` | Job `PATH` when the spec doesn't set one |
| `socket_path` | str\|null | `null` | null = `$H/run/gsched.sock` |
| `log_max_bytes` | int | 5000000 | Rotation size. 3 generations |
| `retention_days` | int | 30 | events, cleared offenders, job dirs of terminal jobs |
| `mcp_max_wait_s` | int | 30 | Cap for `get_job.wait_s` |
| `gpu_busy_foreign_pct` | int\|null | `null` | Experimental. If set: device util above this for 30 s with no GPU job of ours running → protective |
| `launchd_label` | str | `"com.gpuscheduler.agent"` | |
| `default_project` | str\|null | `null` | Used by `gsched run` when `--project` and `$GSCHED_PROJECT` are absent. Must name a loaded project |
| `reservations` | [obj] | `[]` | Time reservations (16 §2). Each: `name` (unique, `^[a-z0-9-]{1,40}$`), `days` (non-empty list of `mon`..`sun`, or `["daily"]`), `start`/`end` (`HH:MM`; `end <= start` wraps midnight, equal = 24 h), `project` (non-empty; need not exist yet), `memory_gib` (> 0), optional `gpu` (`exclusive`\|`shared`\|`none`, default `none`), optional `unload_restartable` (bool, default `false`). Unknown keys are an error naming `reservations[i].key` |
| `default_unknown_estimate_s` | int | 7200 | ≥ 1. Runtime assumed for a job with no `estimate_s` when the reservation overlap rule needs one (16 §2); `gsched exec` also uses it as the estimate of an atomic/cooperative wrapped job that gave no `--est` |
| `job_log_max_bytes` | int | 1073741824 | ≥ 1024. A held daemon-spawned job's `stdout.log`/`stderr.log` in its job dir above this is truncated (16 §3.2) |
| `job_log_keep_bytes` | int | 67108864 | ≥ 0 and ≤ `job_log_max_bytes`. The tail kept in `<name>.1` when a log is truncated |
| `yield_apps` | [obj] | `[]` | Desktop apps the daemon may quit while their memory is needed, and reopens after (QUESTIONS #41). Each: `name` (unique, `^[a-z0-9-]{1,40}$`), `app` (absolute `.app` path), at least one trigger: `reservations` (names of configured reservations; closed while one is active) and/or `for_jobs_gib` (> 0: a queued job reserving at least this, blocked only by `memory_live` for `job_wait_s`, default 60, that closing the app would let fit). Optional: `busy_ports` ([int]; never quit while another process has an ESTABLISHED connection to one), `quit` (`applescript` default \| `sigterm`), `quit_timeout_s` (5..600, default 60; after it the app is left alone for the episode, never SIGKILLed), `reopen` (bool, default `true`), `reopen_delay_s` (default 60) |
| `tcc_protected_roots` | [str] | `[]` | Extra absolute directories (`~` allowed) the daemon must never touch from its own process, in addition to `~/Documents`, `~/Desktop`, `~/Downloads` and `~/Library/Mobile Documents` (07 §2, 11 §4) |

`pressure` object defaults:

```json
{
  "warn":     {"level_below": 30, "swap_rise_gib_per_min": 1.0, "reclaimable_below_gib": 4},
  "critical": {"level_below": 15, "swap_rise_gib_per_min": 4.0, "reclaimable_below_gib": 1.5}
}
```

Any single condition trips its level. Swap rise is measured over a 60 s sliding window of samples. Any key may be `null` to disable that condition.

Example presets ship in `examples/config/`. `workstation.json` is the defaults. `dedicated.json` sets `night_window: null` and `backfill: true`.

## 8. Logging

`gsched/log.py` owns `$H/logs/gsched.log`:

- One line per decision: `2026-11-08T23:00:01.123-08:00 admit job=20261108T225959-a1b2 project=x queue=night mem=20.0GiB gpu=exclusive reclaimable=31.2GiB pending=0.0GiB`.
- Every decision line is also written as an `event` row (see 12). In tests, the log can be disabled while events stay on.
- Rotation: when the file is ≥ `log_max_bytes`, close it, rename `.2`→`.3`, `.1`→`.2`, current→`.1`, and reopen. The daemon only rotates its own file.
- Never log env values, lease tokens, bearer tokens, or command output.
- Commands are logged as `argv0` + argument count, not the full argv: arguments can contain tokens. `gsched get` returns the full command to the same-user caller.
