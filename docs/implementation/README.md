# Implementation guide

These are the build instructions for `gpu-scheduler`. `DESIGN.md` says **what** and **why**. These documents say **how**, in enough detail that you shouldn't need to make policy decisions yourself. If something here contradicts `DESIGN.md`, this guide wins for mechanics and `DESIGN.md` wins for intent. If you find a real contradiction, stop and note it in `docs/implementation/QUESTIONS.md`; don't guess.

## Reading order

| # | Doc | What's in it |
|---|---|---|
| 01 | [Repo, conventions, config](01-repo-and-conventions.md) | Layout, `pyproject.toml`, coding rules, `Clock`, errors, logging, full config reference |
| 02 | [Platform layer (Darwin + fake)](02-platform.md) | Every ctypes binding, verified offsets, the `Platform` protocol, the fake |
| 03 | [Job spec](03-job-spec.md) | Every field, defaults, validation, normalization, error codes |
| 04 | [Storage](04-storage.md) | SQLite schema, transitions and their owners, invariants |
| 05 | [Daemon loop and RPC](05-daemon-and-rpc.md) | Event loop, framing, peer checks, parking, every RPC method |
| 06 | [The tick and policy](06-tick.md) | Phases, eligibility, fit, ranking, fair share, reservation, preemption, morning edge, pressure |
| 07 | [Supervision](07-supervision.md) | Shim, release gate, identity, signals, exit records, reconciliation, external launchers, caffeinate |
| 08 | [Checkpoints and residents](08-checkpoint-and-residents.md) | Cooperative protocol, spill, profiles, `ensure`, health/slots, idle unload, switch |
| 09 | [CLI](09-cli.md) | Commands, flags, output, exit codes |
| 10 | [MCP adapter and AGENTS.md](10-mcp.md) | Framing, lifecycle, tool schemas, the snippet other repos copy |
| 11 | [Install, doctor, packaging](11-install-and-doctor.md) | venv install, LaunchAgent, TCC/Full Disk Access, `doctor`, README outline |
| 12 | [Observability](12-observability.md) | Log format, events, `status` schema, `blocked_by`, history and `suggest` |
| 13 | [Testing](13-testing.md) | Harness, fakes, live tests, the full test catalogue |
| 14 | [PR plan](14-pr-plan.md) | Ten PRs, their files, dependencies, and acceptance criteria |
| 15 | [Dashboard](15-dashboard.md) | PR 11: read-only, loopback-only web dashboard (owner request) |
| 16 | [`gsched exec`, reservations, hardening](16-exec-reservations-hardening.md) | PR 12: fail-open wrapper, time reservations, tick guard, log cap, clock-jump safe timers, TCC guard |

## Ground rules (non-negotiable)

These are the safety invariants. Every PR reviewer checks them.

1. **Never signal a process the daemon did not spawn.** There are no exceptions. External jobs, foreign servers, IDEs, and browsers have no signal path in the code at all. Grep for `os.kill` / `os.killpg`: they may appear only in `gsched/platform/darwin.py` behind `Platform.signal_pid()` and `Platform.signal_group()`, which take a `JobIdentity` and re-verify it immediately before sending.
2. **Never signal an `atomic` or spilled job on policy.** Only `cancel --terminate` (an explicit user action) may stop one.
3. **Commit before you speak.** Any state change that authorizes something (admit, attach, lease) is committed to SQLite before any byte is written to a client socket or to a release gate.
4. **One thread.** The daemon has exactly one thread. No `threading`, no `asyncio`, no `concurrent.futures`. Blocking calls on the tick must have timeouts of 200 ms or less, with health probes on a 1 s timeout handled asynchronously (see 08).
5. **Pure policy.** Everything under `gsched/policy/` is a pure function of its arguments (a `Snapshot`, a `Config`, and `now`). No I/O, no clock reads, no DB access. This is what makes the scheduler testable.
6. **Stdlib only at runtime.** No imports outside the standard library in `gsched/`. `pytest` is the only dev dependency.
7. **No machine-specific constants.** No memsize, page size, core count, path, port, or label literal in `gsched/` except documented config defaults.
8. **Bytes internally.** Convert GiB to bytes at the spec boundary (`int(round(gib * 2**30))`) and convert back only for display. Variables holding memory end in `_bytes`.
9. **Fail closed.** If a probe fails (layout check, swap unreadable, identity unreadable), admit nothing new and signal nothing. Never treat "unknown" as "free".
10. **Observe mode changes nothing.** With `enforce: false`, no row is inserted, nothing is spawned, nothing is signaled.

## Module map

```
gsched/
  __init__.py            __version__ = "0.1.0"
  __main__.py            -> cli.main()
  cli.py                 argparse, client side. Imports mcp_stdio lazily
  client.py              socket client used by cli and mcp_stdio
  config.py              Config dataclass, load/validate/reload
  clock.py               Clock protocol, SystemClock, FakeClock
  errors.py              GschedError hierarchy + error codes
  log.py                 rotating decision log
  ids.py                 job id + lease token generation
  spec.py                parse/validate/normalize job spec (pure)
  projects.py            project + profile file loading
  store.py               sqlite schema, migrations, transition functions
  daemon.py              event loop, RPC dispatch, tick orchestration
  rpc.py                 request/response framing, method table
  tick.py                tick phases (impure executor of policy plans)
  policy/
    __init__.py
    model.py             frozen dataclasses: JobView, RunningJob, Snapshot, Plan
    windows.py           night window, civil dates, fit-window (pure)
    fit.py               resource fit, ledger, blocked_by (pure)
    rank.py              fair share, decay, ranking (pure)
    admit.py             choose_admission, reservation (pure)
    preempt.py           victim planner (pure)
    pressure.py          pressure state + ladder (pure)
    morning.py           morning edge plan (pure)
  supervise.py           spawn via shim, gate, attach, reap, reconcile
  shim.py                the per-job supervisor process (python -I -m gsched.shim)
  scan.py                process classification (uses Platform)
  residents.py           profiles: ensure, health/slots probes, idle, switch
  history.py             history rows, suggest, submit warnings
  status.py              status/get view builders
  mcp_stdio.py           MCP door (imported only by `gsched mcp`)
  install.py             venv + LaunchAgent install/uninstall
  doctor.py              environment checks
  platform/
    __init__.py          Platform protocol, get_platform()
    darwin.py            ctypes implementation
    fake.py              in-memory fake used by tests
    layouts.py           ctypes Structures + offset self-check
deploy/com.gpuscheduler.agent.plist.template
examples/...
tools/check_layouts.c    dev-only: prints sizeof/offsetof from the SDK
tests/...
```

## Definition of done (every PR)

- `python3.12 -m pytest -q` passes on macOS arm64 with **no** network, no GPU workload, and no private repos.
- Tests marked `@pytest.mark.live` (real `ctypes` against the running kernel) pass on an Apple silicon Mac and are skipped elsewhere.
- No new runtime dependency. `python -c "import gsched.daemon, sys; assert 'gsched.mcp_stdio' not in sys.modules"` passes.
- New config keys are in `01-repo-and-conventions.md` §Config, with a default and a validation rule.
- New RPC fields are in `05-daemon-and-rpc.md`.
- No ground rule is violated. The reviewer checks each one explicitly.

## Facts verified on the reference machine (2026-11-08)

Mac Studio, Apple M5 Max, macOS 27.0 (26A425), 48 GiB, Python 3.12.15. Use these to sanity-check your work. **Do not hardcode them.**

| Probe | Result |
|---|---|
| `hw.memsize` | 51539607552 |
| `hw.pagesize` | 16384 |
| `hw.optional.arm64` | 1 |
| `hw.physicalcpu` / `hw.perflevel0.physicalcpu` / `hw.perflevel1.physicalcpu` | 18 / 6 / 12 |
| `iogpu.wired_limit_mb` | 0 (OS default) |
| `sizeof(struct proc_bsdinfo)` | 136. `pbi_uid`@20, `pbi_comm`@48, `pbi_pgid`@100, `pbi_start_tvsec`@120, `pbi_start_tvusec`@128 |
| `sizeof(struct rusage_info_v4)` | 296. `ri_phys_footprint`@72, `ri_lifetime_max_phys_footprint`@240 |
| `struct vm_statistics64` | SDK size 416 / count 104, but **the kernel returned count 40** for a 256-int buffer. Offsets: `free_count`@0, `wire_count`@12, `purgeable_count`@88, `speculative_count`@92, `compressor_page_count`@128, `external_page_count`@136, `internal_page_count`@140 |
| `struct xsw_usage` | 32 bytes. `xsu_total`@0, `xsu_avail`@8, `xsu_used`@16 |
| `SOL_LOCAL` / `LOCAL_PEERCRED` / `LOCAL_PEERPID` | 0 / 1 / 2 |
| `sun_path` | 104 bytes |
| `host_statistics64` (free − speculative) vs `vm_stat` "Pages free" | matched within normal drift between two calls |
| `ioreg -r -d 1 -w 0 -c AGXAccelerator` | `"PerformanceStatistics" = {... "Device Utilization %"=0, "In use system memory"=1115832320, "Alloc system memory"=6489554944 ...}`, `IOClass = AGXAcceleratorG17X` |
