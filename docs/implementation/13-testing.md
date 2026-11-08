# 13: Testing

## 1. Layers

| Layer | Location | Uses | Runs on |
|---|---|---|---|
| Pure policy | `tests/policy/` | `Snapshot` builders, no DB, no daemon | anywhere (Linux CI too) |
| Spec | `tests/spec/` | `parse_spec`, `assign_queue`, `job_env` | anywhere |
| Store | `tests/store/` | real sqlite in tmp | anywhere |
| Daemon integration | `tests/daemon/` | real `Daemon`, real sqlite, real Unix socket, `FakePlatform`, `FakeClock` | macOS or Linux (AF_UNIX); LOCAL_PEERPID is faked through `FakePlatform.peer_credentials` |
| Live | `tests/live/` (`@pytest.mark.live`) | `DarwinPlatform`, real shim, real short processes | Apple silicon only |
| Package | `tests/package/` | file scans, install into tmp home with `--no-load` | macOS |

## 2. Fixtures (`tests/conftest.py`)

```python
@pytest.fixture
def short_tmp():                       # sun_path ≤ 104: never use tmp_path for sockets
    d = tempfile.mkdtemp(prefix="gs-", dir="/tmp"); yield Path(d); shutil.rmtree(d, ignore_errors=True)

@pytest.fixture
def clock():
    # 2026-11-08 12:00:00 in UTC by default; tests that need civil time set cfg.timezone
    return FakeClock(start_ms=1794139200000)

@pytest.fixture
def plat():
    return FakePlatform(memsize_gib=48, cpu=18)

@pytest.fixture
def daemon(short_tmp, clock, plat):
    cfg = Config.defaults().replace(enforce=True, timezone="UTC")
    d = Daemon(home=short_tmp, cfg=cfg, clock=clock, platform=plat)
    d.start(listen=True)
    yield d
    d.stop()

def client(d, method, **params):       # real socket round trip, driving d.step() until the reply arrives
    ...
def run_ticks(d, clock, seconds, step=1.0): # advance + step, no sleeping
    ...
def project(d, name, weight=1, **kw): ...  # writes the project file + reload
def spec(**overrides) -> dict: ...         # minimal valid spec builder
```

`client()` is synchronous: it opens a socket, sends, then loops `d.step(0)` until the response is readable, with a guard of 1000 iterations. **No threads in tests either.**

Snapshot builder for pure tests:

```python
snap = SnapBuilder(memsize_gib=48, reclaimable_gib=30, now="2026-11-08T23:00:00Z") \
         .running(id="A", gpu="exclusive", mem=20, started="22:00", checkpoint="atomic") \
         .queued(id="B", gpu="none", mem=8, priority=0, queue="day") \
         .build(cfg)
assert admit.plan(snap, cfg).admit == "B"
```

## 3. Catalogue by PR

Each PR's tests are listed in its section doc. The canonical scenario tests (names kept from v1 where the behavior is the same) are:

| Scenario | Test | PR |
|---|---|---|
| 24 h atomic doesn't start at 23:00; a 1 h one does | `test_atomic_24h_does_not_start_at_2300` | 4 |
| Two submits, one exclusive holder | `test_two_submits_one_exclusive_holder` | 3 |
| CPU job runs beside a GPU job when memory fits | `test_two_none_gpu_jobs_corun_when_memory_fits` | 4 |
| Admitted-but-not-allocated memory isn't double-booked | `test_pending_prevents_double_admit_before_allocation` | 4 |
| An IDE eating RAM blocks admission without being signaled | `test_memory_live_blocks_when_ide_uses_ram` + `test_browser_footprint_is_record_only` | 4 / 2 |
| Foreign llama-server → protective, never signaled, even when tiny | `test_foreign_heavy_any_footprint_protective_not_signaled` | 5 |
| Our descendant llama-server isn't foreign | `test_descendant_llama_server_is_ours_not_foreign` | 3 |
| Cooperative 08:00: one USR1, no TERM, spill after grace | `test_cooperative_checkpoint_at_0800_not_killed_on_ignore` | 5 |
| Morning handoff is once per civil day | `test_morning_edge_once_per_civil_day` | 5 |
| Atomic never signaled by policy | `test_atomic_never_signaled` (sweep through every policy path) | 5 |
| Higher-priority day job waits during an atomic spill | `test_higher_priority_day_job_waits_during_atomic_spill` | 5 |
| Same priority alternates between projects | `test_same_priority_alternates` | 4 |
| Starving big job gets the reservation | `test_reservation_after_starve_s_blocks_competitors` | 4 |
| Limit counts from process start | `test_limit_starts_at_process_start` | 5 |
| Socket close after submit doesn't cancel | `test_socket_close_after_submit_does_not_cancel` | 3 |
| Commit before response | `test_commit_before_response` | 3 |
| Gate released only after the identity commit | `test_attach_before_gate_release` | 3 |
| Daemon restart mid-job finalizes correctly | `test_live_reconcile_after_daemon_restart` | 3 (live) |
| MCP and CLI produce identical rows | `test_mcp_and_cli_submit_identical_rows` | 8 |
| Observe mode inserts nothing | `test_observe_inserts_nothing` | 3 |
| Checkpoint counter resumes | `test_checkpoint_counter_resumes` | 6 (live) |
| Only the platform sends signals | `test_only_platform_sends_signals` (AST scan for `os.kill`, `os.killpg`, `signal.pthread_kill`, `subprocess` `.kill()`/`.terminate()`/`.send_signal()` outside `platform/darwin.py` and `platform/fake.py` and `shim.py`'s own child) | 1 |
| No personal data | `test_tree_has_no_personal_data` | 10 |

### `test_atomic_never_signaled` (the property test)

For each policy path (morning edge, preempt priority, preempt reservation, pressure critical, protective, idle, limit, ceiling, abort, footprint unreadable ×5, drain, cancel without terminate), build a scenario where an atomic job and a spilled cooperative job are running and the path is maximally triggered. Run 20 ticks. Assert `plat.signals` has no entry for their pids or pgids.

## 4. Live tests (`tests/live/`)

- `test_live_layout_check` — `DarwinPlatform()` constructs without error.
- `test_live_mem_sample_matches_vm_stat` — reclaimable within 5 % of `vm_stat` parsed in the test (two calls close together).
- `test_live_footprint_of_self_positive`, `test_live_identity_of_spawned_sleep`, `test_live_identity_mismatch_after_exit`.
- `test_live_peer_credentials_socketpair`.
- `test_live_port_probe` — listen on an ephemeral port in the test, then probe it.
- `test_live_gpu_stats_parse` — `gpu_stats()` returns ints or all None, never raises.
- Shim tests from 07 §10.
- `test_checkpoint_counter_resumes`.
- `test_live_daemon_end_to_end` — real daemon in a short tmp home, `enforce: true`, submit `python -c "print('hi')"`, `wait` → done, `logs` contains `hi`.

Each live test must finish in under 10 s. Mark slower ones `@pytest.mark.slow` and skip them by default.

## 5. Manual acceptance on a real Mac (before tagging v0.1.0)

1. `gsched install`, then `gsched doctor`: all PASS except the expected WARNs.
2. Observe mode for one day with your normal workload. `gsched status` shows correct offenders. `submit` returns `would`.
3. Enforce. Run `examples/jobs/cpu-only.json` and `examples/jobs/atomic.json` at the same time, and watch `status` show both running.
4. Submit a job with `--mem` larger than the budget → `too_big`. Submit one that fits the budget but not live free memory (open many browser tabs) → `blocked_by: memory_live`.
5. Start a foreign `llama-server` by hand → protective. The GPU job waits. The CPU job still admits. The foreign process is untouched.
6. Cooperative counter: set the night window to end in 2 minutes and watch one SIGUSR1, READY, and the resume tonight (or shift the window again).
7. `launchctl kickstart -k gui/$UID/com.gpuscheduler.agent` while a job runs → the job keeps running, `status` still shows it, and its exit is recorded.
8. MCP from Claude Code: `submit_job`, `get_job` with `wait_s`, `get_job_logs`.
