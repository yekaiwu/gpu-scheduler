# 08: Checkpoints and residents

## 1. Cooperative checkpoint protocol

The contract for programs that declare `checkpoint: cooperative`:

1. On start, read `GSCHED_CHECKPOINT_DIR`. If `GSCHED_RESUME == "1"`, load the latest snapshot from that directory and continue. Otherwise start fresh (and may clear the directory).
2. Optionally, save periodically. That's the program's choice.
3. On `SIGUSR1` (or `checkpoint_signal`): finish or abort the in-flight step, write the snapshot, then write `READY` **last**: the content starts with `ready\n`, then `flush`, then `os.fsync`. Exit with code 0.
4. Never exit 0 with a stale `READY` from an earlier attempt. The daemon guards against this too (below).

Daemon side:

- `request_checkpoint(job, reason)`: before signaling, **delete any existing `READY`** in the checkpoint dir, so a stale marker can't be mistaken for a new one. Then `signal_pid(workload, SIGUSR1)`, `held_reason → checkpointing`, `stop_requested_ms = now`, event. One request per attempt: if `held_reason` is already `checkpointing`, do nothing.
- READY validity at finalize: the file exists, its first 6 bytes are `ready\n`, and `mtime_ns ≥ stop_requested_ms × 10⁶`.
- Success (exit 0 + valid READY) → `requeue_continuation`: same id, `attempt + 1`, `resume = 1`, `enqueued_ms = now`, `queue` re-evaluated with `assign_queue` (a cooperative job checkpointed at 08:00 on the night queue stays on night and resumes tonight), and identity cleared. Charges have already accrued through usage.
- If `cancel_requested` is set at finalize → `cancelled`, and the checkpoint dir is kept.
- Exit 0 without a valid READY → `done` (a normal completion).
- Non-zero exit → `failed`. Not requeued.
- Timeout (`grace_s` after the request): see 06 §11. Default: spill. The job is treated as atomic until it exits, and its `get` view shows `spill_reason: checkpoint_timeout`, plus `blocking_day: true` when outside the night window.

### Reference worker: `examples/checkpoint_counter.py`

Stdlib only, about 60 lines. Behavior:

- Args: `--steps N` (default 600), `--step-s F` (default 1.0).
- State file `$GSCHED_CHECKPOINT_DIR/state.json` = `{"i": <int>}`.
- On resume, load `i`.
- Loop: `i += 1`, `time.sleep(step_s)`, print `step i` with flush. Every 10 steps, write the state atomically.
- `SIGUSR1` handler sets a flag. At the top of the next loop iteration, write the state, write `READY`, fsync, `sys.exit(0)`.
- At `i == steps`, print `done` and exit 0 without writing READY.

`test_checkpoint_counter_resumes` (live, real daemon, real processes, `SystemClock`): submit `checkpoint_counter.py --steps 60 --step-s 0.05` as cooperative with `runtime.limit_s: 2` and `night_window: null`. At about 2 s the limit triggers one `SIGUSR1`. The worker saves (i ≈ 40), writes READY, and exits 0. The daemon requeues the same id. Attempt 2 resumes at the saved `i` and reaches 60 in about 1 s, before its own limit. Assert: one job id, `attempt == 2`, the second attempt's first printed step is the saved `i + 1`, the final state is `done`, and the signal events contain exactly one `SIGUSR1` and no `SIGTERM`.

## 2. Profiles

`$GSCHED_HOME/profiles/<name>.json`:

```json
{
  "name": "llama",
  "project": "local-llm",
  "spec": {
    "command": ["${LLM_ROOT}/start-llama.sh"],
    "cwd": "${LLM_ROOT}",
    "env": {},
    "resources": {"memory_gib": 27, "gpu": "exclusive", "cpu_cores": 2},
    "priority": 10
  },
  "listen_port": 8081,
  "health_url": "http://127.0.0.1:8081/health",
  "slots_url": "http://127.0.0.1:8081/slots",
  "health_bearer_file": null,
  "idle_unload_s": 1800,
  "hold": false,
  "admit": true,
  "reason": null,
  "abort_level_below": null,
  "abort_reclaimable_below_gib": null,
  "abort_swap_rise_gib": null,
  "abort_ceiling_gib": null
}
```

Loading rules (`projects.py: load_profile`):

- `name` = the filename stem. `project` must exist.
- `spec` is a partial job spec. The loader builds the full spec: `schema`, `project`, `checkpoint: "restartable"` (**forced**: a profile is a resident by definition, and any other value → load error), `spawn: "daemon"` (forced), `queue: "day"` (forced; residents are never night jobs), and adds the lock `port:<listen_port>` if `listen_port` is set. Then it runs `parse_spec(door="profile")`.
- `${VAR}` expansion in strings in `spec.command`, `spec.cwd`, `spec.env` values, `health_url`, `slots_url`, and `health_bearer_file`. Variables come from `$H/env` (`KEY=VALUE` lines, `#` comments) first, then the daemon environment. An unset variable → the profile fails to load (`profile_invalid` event, `ensure` → `unknown_profile` with detail). No partial expansion.
- `admit: false` → `ensure` returns `{"view":"refused","reason":…}` and inserts nothing.
- URLs must be `http://127.0.0.1:` or `http://localhost:` or `http://[::1]:`. Anything else → load error. The daemon makes no outbound non-loopback requests.

## 3. `ensure`

```python
def ensure(profile_name, door):
    p = profiles.get(name) or raise unknown_profile
    if not p.admit:            return view("refused", reason=p.reason)
    if not cfg.enforce:        return {"view": "observe", "would": explain_new(p.spec)}
    if p.inhibited:            return view("inhibited")
    row = store.live_profile_row(name)                 # queued or held
    if row:
        store.set_flags(row.id, last_touch_ms=now)
        return view_of(row)                            # running | starting | queued
    same_proj = running restartable job of p.project with profile != name and spawn daemon
    row = store.insert_job(p.spec, profile=name, hold=p.hold, last_touch_ms=now, listen_port=p.listen_port, door="profile")
    if same_proj: store.set_flags(same_proj.id, switch_to=name)   # §6
    admit_hint = True
    return view_of(row)                                 # usually "queued"; the tick admits
```

Views: `running` (held/live and ready), `starting` (held and not ready, or spawning), `queued` (with `blocked_by`), `inhibited`, `refused`, `observe`.

`ensure` never blocks and never spawns on the calling turn. Two concurrent `ensure`s are serialized by the single thread, and the unique index `job_profile_live` guarantees one row.

## 4. Health and slots probes (non-blocking)

The daemon is single-threaded, so it can't call `urllib.request.urlopen` and block. Implement a tiny non-blocking HTTP/1.0 GET in `residents.py`:

- `start_probe(url, bearer)`: non-blocking `socket.connect_ex` to the loopback host:port. Register it in the selector for write. When writable, send `GET /path HTTP/1.0\r\nHost: …\r\n[Authorization: Bearer …]\r\n\r\n` and register for read. Read until EOF or 64 KiB. Parse the status line and the body.
- Timeout: 1 s from the start. On timeout, close: the result is `error`.
- At most one probe in flight per resident.
- The health probe runs every tick while the job is `held/live` and `ready == 0`, and every 10 s after ready (to detect a dead server: 3 consecutive failures after ready → event `health_lost`, and no action unless `abort` is configured). The first 2xx → `ready = 1`, record the **post-ready baseline** (`swap_used`, `level`, `reclaimable`) in memory for the abort lines.
- The slots probe is used only during an idle or preempt unload (§5). The body is JSON (a list of slots). "Busy" = any element with `is_processing == true`. A parse failure = error.
- Bearer: read the file at probe time, strip it, and never log or store it.

## 5. Unloading a resident

The procedure applies to idle unload, preemption, morning, protective, switch, drain, and abort:

1. If `slots_url` is set and the reason is not `pressure`/`protective`/`abort`/`cancel`/`terminate` (those are urgent): enter `stop_pending` (an in-memory per-job state, with `pending_since = now`), probe slots each tick, and wait while busy, up to `preempt_grace_ms`. If slots errors, wait out the grace with event `slots_unavailable`. When it's not busy, or the grace has elapsed, go to step 2.
2. `begin_stop(reason)` → SIGTERM to the group, with escalation to SIGKILL after `kill_grace_s` (06 §11).
3. On exit: `requeue_restartable` (preempt/morning/protective/switch → `queued`, so `ensure` users see `queued`) or a terminal `done` (idle, drain, cancel).

`stop_pending` is in memory only. On a daemon restart it's lost, and the next tick simply re-decides. While `stop_pending`, admission isn't frozen except for reservation-driven stops.

Idle condition: `hold == 0`, `idle_unload_s` set, `ready == 1`, and `now − last_touch_ms ≥ idle_unload_s × 1000`. `restartable_min_hold_s` doesn't apply to idle.

## 6. Same-project profile switch

`ensure(B)` while A (same project, ours, restartable) is `held` sets `A.switch_to = B` and inserts B as `queued`. Maintenance (06 §11) begins A's unload once A has run `restartable_min_hold_s`. B then goes through normal admission. If A and B share `listen_port`, the `port:` lock already prevents overlap. If they don't, they may still overlap briefly if memory allows. Since that's the user's choice of ports, it's acceptable, but the switch still unloads A.

`ensure(A)` while `A.switch_to == B` and B is still `queued`: clear `switch_to` and cancel B (`end_reason=switch_reverted`). Last caller wins.

A different project's `ensure` never sets `switch_to` on another project's resident. It competes through normal admission and preemption.

## 7. Abort lines

Armed only when non-null, and evaluated only after `ready == 1` (against the post-ready baseline):

| Field | Trips when |
|---|---|
| `abort_level_below` | `level < x` |
| `abort_reclaimable_below_gib` | `reclaimable < x GiB` |
| `abort_swap_rise_gib` | `swap_used − baseline_swap > x GiB` |
| `abort_ceiling_gib` | job tree footprint `> x GiB` |

A trip → unload with reason `abort` (urgent, no slots grace) and event `abort_tripped` with the values. If the sample needed for an armed line is unreadable for 30 s → also unload (`abort_sample_unavailable`).

## 8. `inhibit` / `hold`

- `inhibit` sets `profile.inhibited` (persisted in `meta` as `inhibit:<name>`). It doesn't stop a running resident; that's what `cancel` is for. It blocks `ensure` and makes a `queued` resident row ineligible.
- `hold` sets `profile_hold:<name>` in `meta` **and** the live row's `hold`. A held resident skips idle unload and isn't a non-pressure preemption victim.

## 9. Tests (PR 6)

`test_ensure_inserts_once_and_refreshes_touch`, `test_ensure_never_spawns_on_call`, `test_ensure_observe_mode`, `test_profile_forces_restartable_day_daemon`, `test_profile_env_expansion_fails_closed`, `test_profile_non_loopback_url_rejected`, `test_ready_after_health_2xx`, `test_health_probe_nonblocking_timeout`, `test_idle_unload_waits_for_slots_then_terminates`, `test_slots_error_waits_grace`, `test_idle_unload_skipped_when_hold`, `test_same_project_switch_after_min_hold`, `test_switch_revert_cancels_pending`, `test_port_lock_prevents_overlap`, `test_abort_line_unloads_after_ready_only`, `test_inhibit_blocks_ensure`, `test_bearer_never_logged` (grep the log + events), `test_ready_marker_requeues_same_id`, `test_stale_ready_deleted_before_request`, `test_exit_without_ready_is_done`, `test_cooperative_cancel_keeps_checkpoint_dir`, `test_checkpoint_counter_resumes` (live).
