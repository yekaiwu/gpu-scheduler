# 03: Job spec `gpu-scheduler.job.v1`

`gsched/spec.py` is pure. Its job is to turn untrusted JSON into a validated `JobSpec`, or raise `GschedError("schema"|"invalid", ...)`. Checks that need the filesystem or the project registry (`cwd` readable, project exists, command resolves) run in the `submit` handler **after** `parse_spec`, in the order listed in §5.

## 1. Fields

| Path | Type | Required | Default | Rules |
|---|---|---|---|---|
| `schema` | str | yes | | Exactly `gpu-scheduler.job.v1`, else `schema` |
| `project` | str | yes | | `^[a-z0-9][a-z0-9._-]{0,63}$` |
| `name` | str | no | `null` | ≤ 128 chars, printable. A free label used for history grouping and display. Never parsed |
| `command` | [str] | for `spawn: daemon` | | 1..4096 items, each ≤ 32 KiB, no NUL. Total ≤ 256 KiB. `command[0]` non-empty |
| `cwd` | str | for MCP; CLI fills | caller cwd (CLI) / project `default_cwd` (MCP) / else `invalid` | Absolute path after `expanduser`. Stored as `realpath` |
| `env` | {str:str} | no | `{}` | Keys `^[A-Za-z_][A-Za-z0-9_]*$`, ≤ 256 keys, each value ≤ 32 KiB. Keys starting with `GSCHED_` → `invalid` (reserved) |
| `resources.memory_gib` | number | yes | | 0 < x ≤ 4096. Stored as `memory_bytes` |
| `resources.gpu` | str | no | `"exclusive"` | `exclusive`\|`shared`\|`none` |
| `resources.cpu_cores` | int | no | 1 | 1..1024 (capped at `cpu_budget` at admission; > budget → `too_big`) |
| `resources.locks` | [str] | no | `[]` | ≤ 16, each `^[A-Za-z0-9:._/-]{1,128}$`, unique. Prefix `port:` is reserved for profiles → `invalid` from clients |
| `resources.ceiling_gib` | number\|null | no | `null` | > 0. Footprint over this → event `ceiling_exceeded` (and for restartable: unload) |
| `resources.level_min` | int\|null | no | `null` | 0..100 |
| `runtime.estimate_s` | int\|null | no | `null` | > 0, ≤ 30 days |
| `runtime.limit_s` | int\|null | no | `null` | > 0. Counted from process start |
| `checkpoint` | str | **yes** | | `restartable`\|`cooperative`\|`atomic`. No default |
| `priority` | int | no | 0 | −100..100 |
| `queue` | str | no | `"auto"` | `auto`\|`day`\|`night` |
| `after` | [str] | no | `[]` | ≤ 64 job ids, each must exist at submit time (else `invalid`, `detail.unknown`) |
| `spawn` | str | no | `"daemon"` | `daemon`\|`external` |
| `grace_s` | int | no | cfg `grace_s` | 1..86400 |
| `on_timeout` | str | no | cfg `on_timeout` | `wait`\|`kill`. `kill` with `atomic` → `invalid` |
| `allow_spill` | bool | no | cfg `allow_spill` | |
| `idempotency_key` | str\|null | no | `null` | ≤ 128 chars |
| `comment` | str | no | `""` | Truncated to 200 UTF-8 bytes (truncate at a code point boundary). Log-only |
| `stdout` / `stderr` | str\|null | no | `null` | Optional absolute file path. Default is the job dir. Must be in a directory the daemon can write |

Unknown top-level keys → `invalid` with `detail.unknown_keys`. Strictness catches typos like `memory_gb`. Unknown keys inside `resources` or `runtime` → `invalid` too.

Booleans must be JSON booleans and integers must be JSON integers (reject `true` for an int field: `isinstance(x, bool)` comes first, because `bool` is a subclass of `int`). Numbers for `memory_gib` may be int or float. Reject NaN/Infinity. (`json.loads` accepts them by default, so pass `parse_constant` to raise.)

Clients cannot set `resume`, `attempt`, `hold`, `ready`, `switch_to`, or any state column. These names are not in the field table, so they're rejected as unknown keys.

## 2. Normalized form

```python
@dataclass(frozen=True, slots=True)
class JobSpec:
    project: str
    name: str | None
    command: tuple[str, ...] | None
    cwd: str | None
    env: Mapping[str, str]
    memory_bytes: int
    gpu: Literal["exclusive", "shared", "none"]
    cpu_cores: int
    locks: tuple[str, ...]
    ceiling_bytes: int | None
    level_min: int | None
    estimate_s: int | None
    limit_s: int | None
    checkpoint: Literal["restartable", "cooperative", "atomic"]
    priority: int
    queue_requested: Literal["auto", "day", "night"]
    after: tuple[str, ...]
    spawn: Literal["daemon", "external"]
    grace_s: int
    on_timeout: Literal["wait", "kill"]
    allow_spill: bool
    idempotency_key: str | None
    comment: str
    stdout_path: str | None
    stderr_path: str | None

def parse_spec(obj: object, cfg: Config, *, door: Literal["cli", "mcp", "profile"]) -> JobSpec
```

`door` matters for two rules only: MCP refuses `spawn: external` (`forbidden`, message "use the gsched CLI for external launchers"), and `profile` may set `port:` locks.

## 3. Queue assignment (`assign_queue`, pure)

```python
def assign_queue(spec, cfg) -> tuple[str | None, str]:   # (queue, initial_state)
    if cfg.night_window is None:
        return ("day", "queued")         # no fit-window exists, so no estimate is needed
    if spec.checkpoint in ("atomic", "cooperative") and spec.estimate_s is None:
        return (None, "needs_estimate")
    if spec.queue_requested in ("day", "night"):
        return (spec.queue_requested, "queued")
    # auto
    if spec.estimate_s is None:          # restartable without estimate
        return ("day", "queued")
    return ("night" if spec.estimate_s > cfg.day_max_s else "day", "queued")
```

- `estimate_s == day_max_s` stays on `day`.
- An explicit `queue` with a missing estimate on atomic/cooperative still goes to `needs_estimate`, because the fit-window needs the estimate.
- `estimate` RPC: sets `estimate_s`, re-runs `assign_queue`, and moves `needs_estimate → queued`.

## 4. Base environment (`job_env`, pure)

```python
def job_env(spec, job_id, attempt, resume, checkpoint_dir, cfg, user) -> dict[str, str]:
    env = {
        "PATH": cfg.base_path,
        "HOME": user.home, "USER": user.name, "LOGNAME": user.name,
        "SHELL": user.shell, "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    env.update(spec.env)                         # the spec may override PATH etc.
    env.update({
        "GSCHED_JOB_ID": job_id,
        "GSCHED_ATTEMPT": str(attempt),
        "GSCHED_RESUME": "1" if resume else "0",
        "GSCHED_CHECKPOINT_DIR": checkpoint_dir,
        "GSCHED_PROJECT": spec.project,
        "GSCHED_MEMORY_BYTES": str(spec.memory_bytes),
        "PYTHONUNBUFFERED": env.get("PYTHONUNBUFFERED", "1"),   # agents read logs live
    })
    return env
```

`user` comes from `pwd.getpwuid(os.getuid())`. The daemon's `TMPDIR` under launchd is the per-user `/var/folders/...` dir, which is fine.

## 5. Submit-time checks (in the handler, in this order)

1. `parse_spec`: `schema` / `invalid`.
2. Draining → `draining`.
3. Project exists → else `unknown_project` (no row).
4. Door permission: if `door == "mcp"` and not `project.mcp_submit` → `forbidden`. If `project.command_allow` is set, `command[0]` (after resolution, step 7) must start with one of the prefixes → else `forbidden`.
5. `memory_bytes > budget_bytes` → `too_big` (it could never run). `cpu_cores > cpu_budget` → `too_big`. Lock conflicts are not checked here.
4b. **Log paths.** `stdout` / `stderr` (the daemon opens these itself): a path inside a TCC-protected root (below) → `invalid` ("the log path must not be in a TCC-protected root"), `detail.field` names the field.
6. `cwd`. **TCC rule (16 addendum):** under launchd, `open()`/`chdir()`/`listdir()` of a path under `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Library/Mobile Documents` (or `tcc_protected_roots`) does not fail with EPERM: it **blocks** behind a consent prompt, which would freeze the one-thread daemon. So the daemon never touches such a path. It classifies the path lexically (`gsched/tcc.py`: symlinks are followed with `readlink` only *outside* the roots) and consults the per-root access cache, filled by a probe subprocess (`[sys.executable, "-I", "-c", "import os,sys; os.listdir(sys.argv[1]); print('ok')", root]`, polled through the selector, SIGKILLed after 3 s). States: `ok`, `blocked` (timeout), `denied` (EPERM), `absent` (root does not exist), `unknown` (never probed). Probed at startup, every 600 s, and on demand.
   - Path outside every root: `os.listdir(cwd)` as before. `PermissionError` → `cwd_not_accessible` with `detail.hint` = "The daemon can't read this directory. On macOS, folders under ~/Documents, ~/Desktop and ~/Downloads need Full Disk Access for the daemon's Python: <realpath of sys.executable>. Run `gsched doctor` for steps." `FileNotFoundError` → `invalid`.
   - Path inside a root, state `ok`: accepted **without any call on the path** (assumed accessible; the workload's own errors show in its logs). `absent`: the normal checks (nothing can exist there).
   - `unknown`: start the probe if none is running and reply `cwd_not_accessible` with `detail.reason = "tcc_unknown_probe_started"` and a hint to retry in a few seconds. Never blocks.
   - `blocked` / `denied`: `cwd_not_accessible`, `detail.reason = "tcc_blocked"` / `"tcc_denied"`, with a hint naming the consent prompt and Full Disk Access for the realpath of the daemon interpreter.
   - `spawn: external`: the daemon never uses the cwd, so inside a root it is recorded as given and not checked.
7. Command resolution (`spawn: daemon` only): if `command[0]` contains `/`, it must exist and be executable (`os.access(X_OK)`); inside a protected root with state `ok` it is assumed present, with `blocked`/`denied`/`unknown` the same errors as step 6. Otherwise search the job `PATH`: entries inside a protected root are **not searched** (state `ok`: if nothing is found elsewhere the command is assumed to resolve there; otherwise as step 6) → else `command_not_found`. Store the **original** argv. Don't rewrite `command[0]`, because `execvpe` resolves it again at spawn.
8. `after` ids exist → else `invalid`.
9. Idempotency: if `idempotency_key` and a row with `(project, key)` is in `needs_estimate|queued|held`, return that row's `{job, state}` with `"idempotent": true`. Don't insert.
10. Observe mode: return `{"state":"observe","job":null,"would":<explain>,"hint":"gsched is in observe mode (enforce=false); nothing was queued. Enable with: gsched config set enforce true"}`. `would` is computed with the same pure policy functions over the current snapshot as if the job were inserted (06 §9).
11. Insert (store `insert_job`), compute `warnings` from history (12 §6), and reply `{"job": id, "state": "queued"|"needs_estimate", "queue": "...", "warnings": [...]}`.

## 6. Example specs (ship in `examples/jobs/`)

`cpu-only.json`:

```json
{"schema":"gpu-scheduler.job.v1","project":"example-train","name":"tokenize",
 "command":["python3","prep.py"],"cwd":"/path/to/repo",
 "resources":{"memory_gib":12,"gpu":"none","cpu_cores":8},
 "runtime":{"estimate_s":1800},"checkpoint":"restartable"}
```

`cooperative.json` runs `examples/checkpoint_counter.py` with `estimate_s: 86400`, `queue: auto` (→ night).

`pipeline-*.json` documents the `after` pattern. Since ids are only known after submit, the example is a shell script `examples/jobs/pipeline.sh`:

```sh
prep=$(gsched submit --file prep.json --print-id)
train=$(gsched submit --file train.json --after "$prep" --print-id)
gsched submit --file eval.json --after "$train"
```

## 7. Tests (PR 2)

`test_schema_mismatch_is_error_4`, `test_unknown_key_rejected`, `test_bool_is_not_int`, `test_nan_rejected`, `test_checkpoint_required`, `test_kill_rejected_for_atomic`, `test_gsched_env_reserved`, `test_port_lock_reserved_for_profiles`, `test_mcp_rejects_external_spawn`, `test_auto_queue_boundary_7200_is_day`, `test_atomic_without_estimate_needs_estimate`, `test_restartable_may_omit_estimate`, `test_night_window_null_everything_day`, `test_job_env_is_minimal_plus_spec`, `test_comment_truncated_on_codepoint`, `test_memory_gib_to_bytes_exact`.
