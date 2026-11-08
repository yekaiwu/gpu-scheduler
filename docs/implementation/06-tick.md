# 06: The tick and the policy

The tick is the only place where scheduling happens. It's split into **phases**. Each phase builds a pure **plan** from a snapshot (`gsched/policy/*`), and then `gsched/tick.py` **executes** the plan through the store and the platform. Planners never perform I/O, and executors never make policy decisions.

## 0. Models (`policy/model.py`)

```python
@dataclass(frozen=True, slots=True)
class JobView:                # one row, read-only, built from sqlite at tick start
    id: str; project: str; name: str | None; profile: str | None
    state: str; held_reason: str | None
    queue: str | None; priority: int; checkpoint: str; spawn: str
    gpu: str; memory_bytes: int; cpu_cores: int; locks: frozenset[str]
    estimate_s: int | None; limit_s: int | None; allow_spill: bool
    after: tuple[str, ...]; level_min: int | None; ceiling_bytes: int | None
    enqueued_ms: int; eligible_since_ms: int | None
    held_ms: int | None; started_ms: int | None; stop_requested_ms: int | None
    stop_reason: str | None; spilled: bool; checkpoint_failed: bool; cancel_requested: bool
    hold: bool; last_touch_ms: int | None; switch_to: str | None
    footprint_bytes: int | None    # this tick's tree sum; None if unreadable
    grace_s: int; on_timeout: str
    start_waiter_parked: bool      # external only, filled by daemon
    inhibited: bool                # profile inhibited
    project_present: bool; project_weight: int; project_usage: float

@dataclass(frozen=True, slots=True)
class Snapshot:
    now_ms: int
    machine: MachineInfo
    mem: MemSample
    budget_bytes: int
    cpu_budget: int
    jobs: tuple[JobView, ...]      # all non-terminal rows + terminal rows referenced by `after`
    dep_states: Mapping[str, str]  # job id -> state for every id in any `after`
    protective: bool
    pressure: Literal["normal", "warn", "critical"]
    morning_latch: str | None      # meta value
    draining: bool
    last_pressure_action_ms: int | None
```

`running = [j for j in jobs if j.state == "held"]`. Everything below is a function of `(snapshot, cfg)`.

## 1. Tick phases (`tick.py`)

```python
def tick(self, admission_only=False):
    cfg = self.cfg                                  # one config per tick
    now = self.clock.now_ms()
    if not admission_only:
        self.reload_if_changed()                    # config/projects/profiles by mtime
        sample = self.platform.mem_sample(now)      # phase 0: observe
        procs = self.platform.processes()
        scan = self.scanner.classify(procs, self.store.running_jobs(), cfg, now)  # 07 §11
        self.supervisor.reap_and_finalize(now)      # phase 1: reconcile exits (07 §4)
        self.supervisor.check_identities(scan, now) #          lost detection
        self.supervisor.expire_spawn_and_attach(now)
        self.residents.poll(now)                    #          health/slots probes, non-blocking (08)
        self.bookkeep(snapshot_pre, scan, now)      # phase 2: footprints, usage decay+charge, flags (one txn)
    snap = self.build_snapshot(now, sample_or_last, scan_or_last)
    if not admission_only:
        self.execute(policy.maintenance.plan(snap, cfg))   # phase 3: per-job: grace, kill escalation, limits, ceilings, idle, switch, aborts
        self.execute(policy.morning.plan(snap, cfg))       # phase 4: morning edge (latch + actions)
        self.execute(policy.reservations.plan(snap, cfg))  # phase 4b: time-reservation window start (16 §2)
        self.execute(policy.pressure.plan(snap, cfg))      # phase 5: pressure ladder + protective unloads
        self.execute(policy.preempt.plan(snap, cfg))       # phase 6: reservation / priority preemption
        snap = self.build_snapshot(...)                    # cheap rebuild; state changed
    self.execute(policy.admit.plan(snap, cfg))             # phase 7: at most one commit_admit
    self.cascade_dependencies(snap)                        # cancel jobs whose `after` failed
    self.notify_parked()
    self.retention_if_due(now)
```

Rules:

- **Every phase runs under `Ticker.phase(name, fn)` (16 §3.1).** An exception is logged (`tick_error`), the tick skips admission (and admission-only ticks stay closed until a full tick succeeds), and the loop continues. Phases: reload, clock, reconcile, scan, residents, tcc, bookkeep, job_logs, snapshot, maintenance, morning, reservations, pressure, preempt, admit, cascade, notify, retention. If a planner phase fails, the remaining planners are not run on a stale snapshot.
- Phases 3–6 may **signal** and **change state**. Phase 7 only **admits**. In `admission_only` mode only phase 7 runs.
- Any phase may emit "stop admitting this tick" (`plan.freeze_admission = True`). Phase 7 is then skipped. Used when a stop is in flight for the reservation (§6) and during `warn`/`critical` pressure.
- Each executed action writes one `event` row and one log line.
- The tick's wall time is measured. Over 200 ms → event `tick_slow` (rate-limited to once a minute) with per-phase timings.

## 2. Eligibility (`policy/admit.py: eligible`)

A job is **eligible** when it could start now if resources allowed. The `blocked_by` reasons in parentheses are reported in `get`/`status` (12 §5).

```python
def eligible(j, snap, cfg) -> tuple[bool, str | None]:
    if j.state != "queued":                      return False, j.state       # needs_estimate etc.
    if not j.project_present:                    return False, "project_missing"
    if j.inhibited:                              return False, "inhibited"
    if snap.draining and j.profile is not None:  return False, "drain"
    dep = deps_state(j, snap)                    # "ok" | "waiting" | "failed"
    if dep == "waiting":                         return False, "dependency"
    if dep == "failed":                          return False, "dependency_failed"  # cascade cancels it
    if j.spawn == "external" and not j.start_waiter_parked:
                                                 return False, "no_start_waiter"
    if not windows.queue_open(j, snap.now_ms, cfg, day_waiting=day_heavy_waiting(snap, cfg, exclude=j)):
                                                 return False, "window"
    if not windows.atomic_fits(j, snap.now_ms, cfg):
                                                 return False, "fit_window"
    return True, None
```

`day_heavy_waiting` = any other `queued` job with `queue == "day"` that passes every check above except the window check. That function must not call `eligible` recursively: inline the checks.

## 3. Windows (`policy/windows.py`)

```python
def tz(cfg) -> ZoneInfo
def in_night(now_ms, cfg) -> bool                 # civil time, start <= t < end, wrapping midnight
def next_window_end_ms(now_ms, cfg) -> int        # next civil `end` strictly after now
def civil_date(now_ms, cfg) -> str                # YYYY-MM-DD local
def queue_open(j, now_ms, cfg, day_waiting) -> bool:
    if cfg.night_window is None or j.queue == "day": return True
    if in_night(now_ms, cfg): return True
    return cfg.backfill and not day_waiting
def atomic_fits(j, now_ms, cfg) -> bool:
    if cfg.night_window is None or j.queue != "night": return True
    if j.checkpoint != "atomic" or j.allow_spill: return True
    return now_ms + j.estimate_s * 1000 <= next_window_end_ms(now_ms, cfg)
```

One rule covers both cases. Inside the window, the job must end by this morning's `end`. When backfilled outside the window (for example at 16:00), it must end by the **next** `end` (tomorrow 08:00). Running into the night is fine, because that's its queue. Running into the next day isn't.

Civil-time arithmetic: build the local datetime of `start`/`end` on the relevant date with `datetime.combine(date, time, tzinfo=tz)`, then `.timestamp()`. For DST gaps (a nonexistent local time), `zoneinfo` with `fold=0` shifts forward, which is acceptable. Tests: `America/Los_Angeles` on 2026-03-08 (spring forward) and 2026-11-01 (fall back), and `UTC` for the 24 h test.

## 4. Fair share (`policy/rank.py`)

```python
def share(j, snap, cfg) -> float:
    g = {"exclusive": 1.0, "shared": 0.5, "none": 0.0}[j.gpu]
    m = j.memory_bytes / snap.budget_bytes
    c = j.cpu_cores / snap.cpu_budget
    return min(1.0, max(g, m, c))

def decay(usage: float, dt_s: float, halflife_s: int) -> float:
    return usage * 0.5 ** (dt_s / halflife_s)

def charge(projects, running, dt_s, snap, cfg) -> dict[str, float]:
    """new usage per project = decay(old, dt) + Σ share(j) * dt for its running jobs whose gate is released (started_ms set)."""
```

- Bookkeeping (phase 2) computes `dt_s = (now_ms - project.usage_t_ms)/1000` (clamped to [0, 3600] so a clock jump or a long daemon downtime doesn't explode usage), applies `decay` then `charge`, and writes `usage`, `usage_t_ms = now`.
- Jobs that never released their gate (spawning, await_attach) are not charged.
- The rank key, used everywhere:

```python
def rank_key(j, snap):
    return (-j.priority, j.project_usage / j.project_weight, j.enqueued_ms, j.id)
```

`status` shows `usage` and `usage/weight` per project. `test_same_priority_alternates`: two projects, weight 1, four 60 s jobs each, `gpu: exclusive`, so they serialize. Holder order A, B, A, B, A, B, A, B.

## 5. Fit (`policy/fit.py`)

```python
@dataclass(frozen=True)
class Ledger:
    reserved_bytes: int          # Σ max(memory_bytes, footprint or 0) over held
    pending_bytes: int           # Σ max(0, memory_bytes - (footprint or 0)) over held, started or not
    gpu_exclusive: int
    gpu_shared: int
    cpu_used: int
    locks: frozenset[str]

def ledger(snap) -> Ledger

def fits(j, snap, cfg, led: Ledger, *, ignoring: frozenset[str] = frozenset()) -> tuple[bool, list[str]]:
    """`ignoring`: job ids whose resources are treated as already released (used by the preempt planner).
    Returns (ok, blocked_by list in this order)."""
    reasons = []
    if snap.mem.swap_used_bytes is None:                       reasons.append("swap_unknown")
    if snap.pressure != "normal":                              reasons.append("pressure")
    if snap.protective and j.gpu != "none":                    reasons.append("protective")
    if j.level_min is not None and (snap.mem.level is None or snap.mem.level < j.level_min):
                                                               reasons.append("level")
    if led.reserved_bytes + j.memory_bytes > snap.budget_bytes: reasons.append("memory_ledger")
    live = snap.mem.reclaimable_bytes - led.pending_bytes - gib(cfg.headroom_gib)
    if live < j.memory_bytes:                                  reasons.append("memory_live")
    if j.gpu == "exclusive" and (led.gpu_exclusive or led.gpu_shared): reasons.append("gpu")
    if j.gpu == "shared" and (led.gpu_exclusive or led.gpu_shared >= cfg.gpu_shared_max): reasons.append("gpu")
    if led.cpu_used + j.cpu_cores > snap.cpu_budget:           reasons.append("cpu")
    for l in sorted(j.locks & led.locks):                      reasons.append(f"lock:{l}")
    # 16 §2: reservation_window:<name> for every time reservation that holds back memory or the GPU this job needs
    return (not reasons), reasons
```

Notes:

- **Why both memory checks.** The ledger stops us committing more than the budget to our own jobs. The live check stops us starting a job when something else (an IDE, a foreign server, a job over its reservation) has already used the memory. With `pending`, a job admitted one tick ago that hasn't allocated yet still counts.
- **The `ignoring` variant** (for preemption): subtract the ignored jobs from the ledger, and for the live check **add back** their current footprint (`+ footprint_bytes`) as memory that will become reclaimable. Under-estimate on purpose: add back `0.9 × footprint`.
- A job's footprint is None when unreadable. In the ledger, count it as `memory_bytes`, and in pending, as 0 pending (conservative both ways). A `held` job that has **never been measured** (spawning, or admitted after the last scan, which is every job admitted by the admission-only tick that follows a submit) is not "unreadable": it has touched nothing, so its footprint is 0 and all of its reservation is pending. Otherwise the next admission passes the live check against a sample taken before the first job existed.

## 6. Admission (`policy/admit.py: plan`) — phase 7

```python
def plan(snap, cfg) -> Plan:
    if not cfg.enforce: return Plan.empty()
    led = ledger(snap)
    cands = sorted((j for j in snap.jobs if eligible(j, snap, cfg)[0]), key=lambda j: rank_key(j, snap))
    reservation = current_reservation(cands, snap, cfg)     # see below
    for j in cands:
        ok, why = fits(j, snap, cfg, led)
        if ok:
            if reservation is not None and j.id != reservation.id and competes(j, reservation, why_res):
                continue                                     # would delay the reservation
            return Plan(admit=j.id)
    return Plan.empty(blocked={j.id: fits(j, snap, cfg, led)[1] for j in cands})
```

- **One admit per tick.** The next tick re-samples memory before admitting the next job.
- **Reservation:** the highest-ranked candidate whose `eligible_since_ms` is at least `starve_s` old and that doesn't fit. Only one reservation exists at a time. `eligible_since_ms` is set by bookkeeping the first tick a job is eligible but doesn't fit, and cleared when it is admitted or becomes ineligible.
- `competes(j, res, res_reasons)`: true if `j` would consume any resource the reservation is blocked on. `memory_*` → any `j` with `memory_bytes > 0` competes (that's every job, so a memory-blocked reservation drains the machine). `gpu` → `j.gpu != "none"`. `cpu` → always. `lock:X` → `X in j.locks`. Non-resource reasons (`level`, `pressure`, `protective`, `swap_unknown`) don't create a reservation at all: those conditions aren't freed by waiting for jobs to finish.
- Executing `Plan(admit=id)`: `store.commit_admit(id, now)`. If `rowcount == 1`: daemon spawn → `supervisor.spawn(job)` (07). External → reply the lease to the parked start waiter. Then set `admit_hint` so the next loop iteration can admit another job if the ledger allows (still one per `tick` call).

### 6.1 Higher-priority jobs vs. backfill

Because `cands` is sorted by priority first, a priority-10 job that fits is always admitted before a priority-0 job. A priority-10 job that doesn't fit does **not** block a priority-0 job that fits, unless it's the reservation, or unless the preemption planner (§8) has started victims for it (then it's the reservation with `freeze_admission` set).

## 7. Morning edge (`policy/morning.py`) — phase 4

```python
def plan(snap, cfg) -> Plan:
    if cfg.night_window is None: return Plan.empty()
    today = civil_date(snap.now_ms, cfg)
    if snap.morning_latch == today or in_night(snap.now_ms, cfg): return Plan.empty()
    p = Plan(set_meta={"morning_latch": today})        # latch even if nothing else happens
    for j in running(snap):
        if j.checkpoint == "cooperative" and j.queue == "night" and j.held_reason == "live" \
           and not j.spilled and not j.checkpoint_failed:
            p.checkpoint.append((j.id, "morning"))
    if cfg.morning_unload_restartable:
        target = top_day_waiter(snap, cfg)            # highest-ranked eligible queued day job that doesn't fit
        if target:
            victims = preempt.find_victims(target, snap, cfg, mode="morning")
            p.stop += [(v, "morning") for v in victims]
            if victims: p.reserve = target.id; p.freeze_admission = True
    return p
```

- The latch is written in the same transaction as the actions.
- A daemon that starts at 10:00 with no latch for today takes the edge on its first tick. That is intended.
- `admission_only` ticks never run this.
- Day-queue jobs running at the edge are not touched. A cooperative job on `queue: day` was admitted as a ≤ `day_max_s` job and keeps running.

## 8. Preemption (`policy/preempt.py`) — phase 6

```python
def find_victims(target, snap, cfg, mode: Literal["priority", "reservation", "morning", "pressure"]) -> list[str]:
    cands = []
    for v in running(snap):
        if v.spawn != "daemon" or v.held_reason not in ("live",): continue
        if v.checkpoint == "atomic" or v.spilled: continue
        held_s = (snap.now_ms - v.started_ms) / 1000
        if v.checkpoint == "restartable":
            if mode in ("priority", "reservation") and not cfg.preempt_restartable: continue
            if held_s < cfg.restartable_min_hold_s and mode != "pressure": continue
            if mode == "priority" and not v.priority < target.priority: continue
            if mode == "reservation" and not v.priority <= target.priority: continue
            if v.hold and mode != "pressure": continue
        elif v.checkpoint == "cooperative":
            if mode == "morning": continue                     # morning handles cooperatives separately
            if mode == "pressure" and not cfg.pressure_checkpoint_cooperative: continue
            if mode != "pressure" and not v.priority < target.priority: continue
            if held_s < cfg.coop_min_hold_s and mode != "pressure": continue
        cands.append(v)
    # restartable before cooperative, then lowest priority, then highest project usage/weight, then newest started
    cands.sort(key=lambda v: (v.checkpoint != "restartable", v.priority, -v.project_usage / v.project_weight, -v.started_ms))
    chosen = []
    for v in cands:
        chosen.append(v.id)
        if fits(target, snap, cfg, ledger(snap), ignoring=frozenset(chosen))[0]:
            return minimize(chosen, target, snap, cfg)          # drop any victim not needed (greedy reverse pass)
    return []                                                   # can't make it fit -> signal nobody

def plan(snap, cfg) -> Plan:
    if not cfg.enforce: return Plan.empty()
    if any(j.held_reason in ("stopping", "checkpointing") and j.stop_reason in ("preempt", "reservation") for j in running(snap)):
        return Plan(freeze_admission=True)                      # a preemption is already in flight
    cands = sorted(eligible queued jobs, key=rank_key)
    target = first cand that doesn't fit and either
             (a) is the reservation, or
             (b) has priority > max(priority of running daemon-spawned jobs it would need to displace)
    if not target: return Plan.empty()
    victims = find_victims(target, snap, cfg, mode="reservation" if target is reservation else "priority")
    if not victims: return Plan.empty()
    return Plan(stop=[(v, "preempt") for v in victims if restartable],
                checkpoint=[(v, "preempt") for v in victims if cooperative],
                reserve=target.id, freeze_admission=True)
```

Key properties to test:

- Atomic, spilled, external, and `hold` residents are never victims (except `hold` under pressure).
- Equal priority never preempts a cooperative job (`test_equal_priority_does_not_checkpoint_cooperative`).
- A cooperative job held 299 s isn't asked. At 300 s it gets exactly one SIGUSR1 (`test_higher_priority_checkpoints_cooperative_after_coop_min_hold`).
- If the victims can't make the target fit, nothing is signaled (`test_no_pointless_preemption`).
- While victims stop, admission is frozen. Afterwards the target is admitted first because it is still top-ranked, and the freed space can't go to a lower job (`test_preempted_space_goes_to_target`).
- When the target disappears (cancelled) while victims are stopping, the stop continues (restartables) or the checkpoint completes (cooperatives requeue). No rollback.

## 9. Observe-mode `would` (`policy/admit.py: explain_new`)

For an observe-mode submit, build a hypothetical `JobView` (state `queued`, `enqueued_ms = now`), add it to a copy of the snapshot, and run `eligible` + `fits`. Return one of:

- `{"would":"admit_now"}`
- `{"would":"queue","blocked_by":[...],"why":"..."}`
- `{"would":"needs_estimate"}`

Observe mode keeps no queue, so this is a point-in-time answer. The `hint` says so.

## 10. Pressure (`policy/pressure.py`) — phase 5

```python
def level(snap, history: SwapWindow, cfg) -> Literal["normal","warn","critical"]:
    # any single condition trips; critical checked first
    # swap rise: (swap_used_now - swap_used_60s_ago) / GiB per minute
def plan(snap, cfg) -> Plan:
    p = Plan()
    if snap.pressure != "normal": p.freeze_admission = True
    if snap.pressure == "critical" and (snap.last_pressure_action_ms is None
            or snap.now_ms - snap.last_pressure_action_ms >= cfg.pressure_step_s * 1000):
        vs = victims_for_pressure(snap, cfg)                 # lowest priority restartable we own; then cooperative if enabled
        if vs: p.stop or p.checkpoint the first one; p.set_meta last_pressure_action_ms
        else: p.alarm("pressure_critical_no_action")         # rate limit: once per 5 min
    if snap.protective and cfg.protective_unload_restartable:
        for j in running(snap):
            if j.checkpoint == "restartable" and j.spawn == "daemon" and j.gpu != "none" and j.held_reason == "live":
                p.stop.append((j.id, "protective"))
    return p
```

The pressure level is computed in bookkeeping (phase 2) from the sample plus a 60-entry ring buffer of `(t_ms, swap_used)` kept in memory (lost on restart, which is fine). It is stored on the snapshot. Transitions emit `pressure_enter` / `pressure_exit` events.

If swap is unreadable: `pressure` is `warn` (fail closed: no admits) and event `swap_unknown`.

## 11. Per-job maintenance (`policy/maintenance.py`) — phase 3

For every `held` job, in this order (first match wins per job per tick):

| Condition | Action |
|---|---|
| `held_reason == "stopping"` and `now − stop_requested_ms ≥ kill_grace_s` and identity matches and SIGKILL not yet sent | `signal_group(SIGKILL)` once, then set `kill_sent = 1` on the row |
| `held_reason == "checkpointing"` and `now − stop_requested_ms ≥ grace_s` | `on_timeout == "wait"` → `mark_spilled(reason="checkpoint_timeout")`. `kill` → `begin_stop("checkpoint_timeout_kill")` + SIGTERM group, with event `work_lost_since_last_ready` |
| `held_reason == "live"`, `limit_s` set, `now − started_ms ≥ limit_s × 1000` | restartable (ours) → `begin_stop("limit")`. cooperative → `request_checkpoint("limit")`. atomic/spilled → `over_limit = 1` (once). external → `yield_requested = 1` |
| `ceiling_bytes` set and `footprint > ceiling` | event `ceiling_exceeded` (once per job attempt). restartable (ours) → `begin_stop("ceiling")` |
| `estimate_s` set, `now − started_ms > estimate_s × 1000` | `estimate_overrun = 1` (once), event |
| restartable profile job, `hold == 0`, `idle_unload_s` set, `now − last_touch_ms ≥ idle_unload_s × 1000`, `ready` | start idle unload (08 §5: the slots grace first) |
| restartable profile job with `switch_to` set and held ≥ `restartable_min_hold_s` | `begin_stop("switch")` (08 §6) |
| profile abort lines armed and tripped (08 §7) | `begin_stop("abort")` |
| `footprint_fail_n ≥ 5` (consecutive None) | restartable (ours) → `begin_stop("footprint_unreadable")`. Others → event `footprint_unreadable` once, no signal |

`begin_stop` always sends `SIGTERM` to the group immediately (identity-checked) and records `stop_requested_ms`. Resident unloads with a `slots_url` go through 08 §5 first.

## 12. Dependency cascade

After phase 7: for every `queued`/`needs_estimate` job with an `after` id whose state is `failed`, `cancelled`, or `lost` → `cancel_unstarted(end_reason="dependency_failed")`. This repeats transitively on the next ticks (or loop until there are no changes; the depth is bounded by the job count).

## 13. Tests (PR 4 = §2–6, §9, §12; PR 5 = §7, §8, §10, §11)

PR 4: `test_one_exclusive_gpu_job`, `test_two_none_gpu_jobs_corun_when_memory_fits`, `test_shared_gpu_max`, `test_memory_ledger_blocks`, `test_memory_live_blocks_when_ide_uses_ram`, `test_pending_prevents_double_admit_before_allocation`, `test_one_admit_per_tick`, `test_atomic_24h_does_not_start_at_2300` (a second 3600 s night job IS selected), `test_backfill_default_false`, `test_backfill_true_runs_night_job_when_no_day_waiting`, `test_day_job_eligible_at_night`, `test_night_window_uses_civil_time` (both DST transitions), `test_night_window_null`, `test_same_priority_alternates`, `test_priority_beats_usage`, `test_higher_priority_does_not_bypass_memory`, `test_small_job_backfills_past_big_blocked_job`, `test_reservation_after_starve_s_blocks_competitors`, `test_reservation_not_created_for_pressure_or_protective`, `test_lock_conflict`, `test_external_not_eligible_without_start_waiter`, `test_gate_uses_budget_not_memsize_constant` (16 GiB and 512 GiB fake machines), `test_dependency_waits_and_cascades`, `test_pick_is_pure` (calling `plan` twice on the same snapshot gives the same result, and nothing changes), `test_usage_decays_halflife`, `test_usage_dt_clamped`.

PR 5: `test_morning_edge_once_per_civil_day`, `test_daemon_started_at_1000_takes_edge_once`, `test_morning_checkpoints_night_cooperative_not_day`, `test_morning_unloads_restartable_even_if_preempt_off`, `test_morning_never_signals_atomic`, `test_preempt_restartable_flag_off_no_fairness_preempt`, `test_equal_priority_does_not_checkpoint_cooperative`, `test_higher_priority_checkpoints_cooperative_after_coop_min_hold`, `test_no_pointless_preemption`, `test_preempted_space_goes_to_target`, `test_hold_resident_not_victim_except_pressure`, `test_pressure_warn_freezes_admission`, `test_pressure_critical_unloads_lowest_restartable_one_per_step`, `test_pressure_never_signals_atomic`, `test_swap_unknown_fails_closed`, `test_limit_starts_at_process_start` (queued 10 fake hours, `limit_s` 3600, not expired until 3600 s after `started_ms`), `test_limit_atomic_marks_over_limit_no_signal`, `test_kill_escalation_after_kill_grace`, `test_ceiling_unloads_restartable_only`, `test_footprint_unreadable_5x`.
