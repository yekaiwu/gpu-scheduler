# 12: Observability

## 1. Event kinds

Every decision is one `event` row, `{t_ms, kind, job, body_json}`, plus a log line. Kinds (closed set; add new ones here):

| Kind | Body |
|---|---|
| `submit` | project, name, queue, priority, gpu, memory_bytes, checkpoint, door |
| `admit` | ledger before/after, reclaimable, pending, rank position, reservation? |
| `spawn` / `spawn_error` / `spawn_abort` | shim pid, reason |
| `gate_released` | pid, path |
| `signal` | pid or pgid, signal name, reason |
| `checkpoint_request` / `checkpoint_ok` / `spill` | reason, grace_s |
| `stop` | reason (preempt, morning, pressure, protective, idle, switch, limit, ceiling, abort, cancel, terminate, drain) |
| `exit` | exit_code/signal, runtime_s, peak_footprint_bytes, final state |
| `lost` | why |
| `requeue` | attempt, reason |
| `preempt_plan` | target, victims |
| `reservation_set` / `reservation_clear` | job |
| `morning_edge` | date, actions |
| `pressure_enter` / `pressure_exit` | level, triggering values |
| `protective_enter` / `protective_exit` | offenders |
| `offender` | pid, path, port, class, footprint |
| `estimate_overrun`, `over_limit`, `ceiling_exceeded`, `footprint_unreadable`, `health_lost`, `slots_unavailable`, `abort_tripped` | values |
| `sample` | once a minute: level, reclaimable parts, swap, budget, reserved, pending, GPU util, running count |
| `tick_slow` | per-phase ms |
| `tick_error` / `tick_error_persistent` | phase, streak, error, traceback tail. A phase raised; the tick failed closed (no admission). Written for the first 3 consecutive failures of a phase and then every 10th; `_persistent` every 20th consecutive tick (16 §3.1) |
| `loop_error` | error, traceback tail. A bug outside the tick phases (a selector callback) was caught in `serve_forever` |
| `clock_skew` | skew_ms, shifted. Wall time moved by more than 30 s relative to monotonic time (sleep, NTP step); grace/kill timers of stopping/checkpointing jobs were shifted (16 §3.3) |
| `job_log_truncated` | job, stream, size_bytes, kept_bytes (16 §3.2) |
| `reservation_start` | reservation, date, stop, checkpoint. The window-start edge ran (once per reservation per civil date) |
| `reservation_blocked_by_atomic` | reservation, jobs, gib. Atomic/spilled/external jobs of other projects still hold what the window needs; nothing was signaled |
| `exec_max_wait` | job, waited_s. `gsched exec --max-wait` gave up and cancelled the queued job |
| `tcc_probe` | root, state, detail. The cached access state of a TCC-protected root changed (03 §5 step 6) |
| `cwd_blocked` | job, cwd, timeout_ms. The workload never reported `cwd_ok`; the job is stopped and ends `failed/cwd_blocked` (07 §2) |
| `health_bearer_refused` | job, why. A profile's bearer file is inside a protected root and was not opened |
| `config_reload` / `config_reload_failed` / `config_unknown_key` | key, error |
| `memsize_changed` | old, new |
| `daemon_start` / `daemon_stop` | version, pid, reconcile summary |

No env values, leases, bearer tokens, or command arguments appear in any event.

## 2. Log line format

`<ISO8601 local with offset> <kind> key=value ...` with values quoted when they contain spaces. Memory is printed as `20.0GiB`. Example:

```
2026-11-08T08:00:00.412-08:00 morning_edge date=2026-11-08 checkpoint=[20261107T231000-a1b2] stop=[]
2026-11-08T08:00:00.413-08:00 signal job=20261107T231000-a1b2 pid=48121 sig=SIGUSR1 reason=morning
```

## 3. `status` schema

```json
{
  "version": "0.1.0", "daemon_pid": 123, "uptime_s": 3600, "enforce": true, "draining": false,
  "machine": {"chip":"Apple M5 Max","memsize_bytes":51539607552,"memsize_gib":48.0,"physical_cpu":18,
              "perf_cores":6,"eff_cores":12,"os":"27.0","gpu_wired_limit_mb":0},
  "memory": {"budget_bytes":…, "budget_gib":40.0, "reserved_gib":32.0, "pending_gib":3.1,
             "reclaimable_gib":12.8, "live_free_gib":7.7,
             "reclaimable_parts_gib":{"free":4.4,"purgeable":0.7,"file_backed":7.7},
             "level":85, "swap_used_gib":2.58, "swap_total_gib":4.0, "swap_known":true},
  "gpu": {"exclusive_job":"…|null","shared_jobs":[], "device_util_pct":97, "in_use_gib":26.1},
  "cpu": {"budget":18,"used":6},
  "pressure": "normal", "protective": false, "sleep_prevented": true,
  "night_window": {"start":"23:00","end":"08:00","tz":"America/Los_Angeles","in_night":false,"next_end":"…"},
  "running": [ {job view, brief} ],
  "queue":   [ {job view, brief, with blocked_by/why} ],
  "reservation": "…|null",
  "reservations": [{"name":"t233-pass","project":"latent-alpha","days":["mon","tue"],"start":"19:00","end":"23:30",
                    "memory_gib":27.0,"gpu":"exclusive","unload_restartable":true,"active":true,
                    "next_start":"…","next_end":"…","next_start_ms":0,"next_end_ms":0,
                    "reserve_remaining_gib":17.0,"owner_jobs":["…"]}],
  "projects": [{"id":"example-train","weight":1,"usage":5321.4,"usage_per_weight":5321.4,"present":true}],
  "offenders": [{"pid":4242,"path":"/opt/homebrew/bin/llama-server","class":"foreign_heavy","footprint_gib":16.0,"port":8089,"since":"…"}],
  "warnings": [{"kind":"estimate_overrun","job":"…","t":"…"}]
}
```

`reservations`: one entry per configured reservation (also in `--brief`). `next_start`/`next_end` are the bounds of the window that is active now, else of the next one; `reserve_remaining_gib` is the reservation minus the memory of the owner project's held jobs; `owner_jobs` are those jobs. For `door: dashboard` each entry also has `windows: [{start_ms,end_ms}]` (overlapping now ± 36 h), which the dashboard shades on its timeline; the header strip shows an active window as a `window <name> until HH:MM` badge and an upcoming one within 24 h as `window <name> in <dur>`. `status --brief` appends `· reservation <name> active until HH:MM` for each active window.

`status --brief` / MCP `get_status` is a subset: machine.memsize_gib, the memory numbers, gpu, pressure, protective, enforce, running (id, project, name, mem, gpu, runtime), and queue counts per `blocked_by`.

`warnings` lists the latest instance of each warning kind from the last 24 h: protective, spill, `blocking_day`, `estimate_overrun`, `over_limit`, `footprint_unreadable`, `abort_tripped`, `lost`, `tick_slow`, `pressure_critical_no_action`, `health_lost`, `config_reload_failed`, `tick_error`, `tick_error_persistent`, `clock_skew`, `job_log_truncated`, `reservation_blocked_by_atomic`, `cwd_blocked`.

## 4. Job view (`get`)

```json
{
  "job":"20261108T231500-7f3a","project":"example-train","name":"sweep-lr",
  "state":"queued","held_reason":null,"queue":"night","priority":0,"checkpoint":"atomic","spawn":"daemon",
  "resources":{"memory_gib":20.0,"gpu":"exclusive","cpu_cores":4,"locks":[]},
  "runtime":{"estimate_s":14400,"limit_s":null},
  "attempt":1,"resume":false,
  "submitted":"…","enqueued":"…","held":null,"started":null,"released":null,
  "blocked_by":["window"],"why":"night-queue job; the night window opens at 23:00 (in 6h12m). backfill is off.",
  "position":2,
  "footprint_gib":null,"peak_footprint_gib":null,
  "flags":{"spilled":false,"checkpoint_failed":false,"over_limit":false,"cancel_requested":false,
           "yield_requested":false,"estimate_overrun":false,"blocking_day":false},
  "exit":{"code":null,"signal":null,"end_reason":null},
  "pid":null,"command":["python3","train.py"],"cwd":"/…","env_keys":["WANDB_MODE"],
  "logs":{"stdout":"/…/stdout.log","stderr":"/…/stderr.log"},
  "after":[], "warnings":[]
}
```

`command` and `cwd` are included in `get` (same-user caller) but not in `list` or the status lists.

## 5. `blocked_by` and `why`

`status.py: explain(job, snap, cfg)` returns the reason codes (06 §2 eligibility first, then 06 §5 fit) plus one human sentence built from templates:

| Code | `why` template |
|---|---|
| `needs_estimate` | "atomic/cooperative jobs need runtime.estimate_s; run `gsched estimate {id} 3h`" |
| `dependency` | "waiting for {n} job(s): {ids}" |
| `dependency_failed` | "dependency {id} ended {state}; this job will be cancelled" |
| `no_start_waiter` | "external job: run `gsched wait {id} --until start` to receive the lease" |
| `window` | "night-queue job; the night window opens at {start} (in {dur}). backfill is {on/off}" |
| `fit_window` | "atomic, estimate {est} > {left} left before {end}. Set allow_spill or use queue=day to run anyway" |
| `memory_ledger` | "needs {need} GiB; {reserved} of {budget} GiB budget is reserved by {n} running job(s)" |
| `memory_live` | "needs {need} GiB + {headroom} headroom; only {free} GiB is free right now (other apps use memory)" |
| `gpu` | "GPU is taken by {job} ({mode})" |
| `cpu` | "needs {n} cores; {used}/{budget} in use" |
| `lock:X` | "lock {X} is held by {job}" |
| `protective` | "a foreign GPU-heavy process is running ({path} pid {pid}); GPU jobs wait until it exits" |
| `pressure` | "memory pressure is {level}; admissions are paused" |
| `swap_unknown` | "swap usage is unreadable; admissions are paused (fail-closed)" |
| `level` | "needs memorystatus level ≥ {min}; now {level}" |
| `reservation` | "job {id} has waited {dur} and holds the reservation; this job would delay it" |
| `reservation_window:<name>` | "the <name> window (<start>–<end>) reserves <mem> GiB[ and the GPU| and a shared GPU slot] for <project>; this job would overlap it (ends ~<HH:MM>)". The job would use memory or the GPU that the reservation holds back, either because the window is active or because the job (estimate, else `default_unknown_estimate_s`) would still be running when it opens. Not a resource a running job will free, so it never creates the starvation reservation. Restartable jobs without an estimate are exempt from the overlap rule (16 §2) |
| `inhibited` / `drain` / `project_missing` | obvious |

`position` = 1-based rank among eligible queued jobs.

## 6. History and suggestions (`history.py`)

On each finished attempt (done, failed, requeue continuation, restartable requeue), insert a `history` row with `runtime_s = (released_ms − started_ms)/1000` (null if never started) and the peak footprint.

`suggest(project, name)`: the last 50 rows for `(project, name)`, or `(project, *)` if `name` is null. Uses `outcome in (done, requeue)` only. Returns `memory_gib_p95 = ceil_to_0.5(p95(peak) × 1.10 / GiB)`, `peak_gib_max`, `runtime_s_p50`, `runtime_s_p95`, `n`. Fewer than 3 rows → `{"n": k, "note": "not enough history"}`.

Submit `warnings` (non-blocking):

- `memory_gib < p95 peak` (n ≥ 3): "memory_gib {x} is below the p95 peak of {p} GiB for {project}/{name}".
- `estimate_s < p50 runtime × 0.5` (n ≥ 3): "estimate looks short (p50 {d})".
- `gpu == "exclusive"` and `memory_gib ≥ 0.75 × memsize` with `gpu_wired_limit_mb == 0`: "may exceed the default Metal wired-memory limit; see `gsched doctor`".

## 7. Tests (with the relevant PRs)

`test_every_decision_writes_event`, `test_no_env_values_in_log_or_events`, `test_status_schema_keys`, `test_status_does_not_mutate` (row and meta checksums before and after), `test_blocked_by_for_each_code` (table-driven), `test_suggest_p95`, `test_submit_warns_low_memory_vs_history`, `test_log_rotation_three_generations`, `test_sample_event_once_per_minute`.
