# 14: PR plan

Ten PRs. Each merges on its own, leaves `pytest -q` green, and adds no runtime dependency. Do them in order unless the dependency column allows parallel work. If a PR grows past about 1500 lines of non-test code, split it, and keep each part green.

| PR | Title | Depends on | Can run in parallel with |
|---|---|---|---|
| 1 | Skeleton, config, platform layer | — | — |
| 2 | Spec, store, projects, scan classification | 1 | — |
| 3 | Daemon loop, RPC, supervision, observe mode | 2 | — |
| 4 | Scheduling policy: eligibility, windows, fit, fair share, admission | 3 | 8 (after 3) |
| 5 | Maintenance, morning edge, preemption, pressure, protective | 4 | 7 |
| 6 | Cooperative checkpoints and residents | 5 | 7 |
| 7 | Full CLI, history, suggest | 3 (+4 for `explain`) | 5, 6, 8 |
| 8 | MCP adapter + AGENTS.md | 3 | 4–7 |
| 9 | Install, doctor, README, examples | 6, 7, 8 | — |
| 10 | Publishing hygiene, CI, v0.1.0 | 9 | — |
| 11 | Read-only dashboard (`gsched dashboard`), `events` RPC (spec: 15-dashboard.md) | 7 | 8–10 |
| 12 | `gsched exec`, time reservations, hardening, `install --venv-symlinks`, TCC guard (spec: 16-exec-reservations-hardening.md) | 11 | — |

---

## PR 1: Skeleton, config, platform layer

**Files:** `pyproject.toml`, `LICENSE`, `.gitignore`, `CONTRIBUTING.md`, `gsched/__init__.py`, `__main__.py` (stub CLI with `version`), `units.py`, `clock.py`, `errors.py`, `config.py`, `log.py`, `ids.py`, `platform/{__init__,darwin,fake,layouts}.py`, `tools/check_layouts.c`, `tests/conftest.py`, `tests/platform/*`, `tests/live/test_platform_live.py`.

**Do:**
- Everything in 01 §2–8 and all of 02. The fake must be complete enough for PRs 2–6 (memory knobs, foreign processes, the fake shim/workload protocol over real pipes, the signal log, recycle_pid, ports).
- `Config.load(path)` with full validation and `Config.defaults()`. Also `Config.replace(**kw)` for tests.

**Tests:** `test_units_gib_exact`, `test_config_defaults_match_doc` (a literal dict in the test mirrors the 01 §7 table, so a reviewer can diff the two), `test_config_invalid_value_names_key`, `test_config_unknown_key_logged_once`, `test_fake_clock_never_sleeps`, `test_only_platform_sends_signals`, `test_fake_signal_log`, `test_fake_identity_recycle`, plus the live tests in 13 §4 that touch only the platform.

**Acceptance:** `python -c "from gsched.platform import get_platform; p=get_platform(); print(p.machine(), p.mem_sample(0).reclaimable_bytes/2**30)"` prints sane values on an Apple silicon Mac.

## PR 2: Spec, store, projects, scan classification

**Files:** `spec.py`, `store.py`, `projects.py` (projects only; profiles in PR 6), `scan.py` (classification only, no protective actions), `tests/spec/*`, `tests/store/*`, `tests/scan/*`.

**Do:** 03 completely (except submit-handler steps that need the daemon: write them as functions `check_cwd`, `resolve_command`, `check_permissions` that PR 3 calls). 04 completely. 07 §11 steps 1–6 (KERN_PROCARGS2 optional). The `FIELDS` table in `spec.py` drives validation (10 §5 needs it later).

**Tests:** those listed in 03 §7, 04 §8, and the classification tests in 07 §11.

## PR 3: Daemon loop, RPC, supervision, observe mode

**Files:** `daemon.py`, `rpc.py`, `tick.py` (phases 0, 1, 2, and 7 with a **temporary** admission: first eligible job by `(-priority, enqueued_ms)` that fits per 06 §5; PR 4 replaces it), `supervise.py`, `shim.py`, `client.py`, `gsched/launcher.py`, minimal `cli.py` (`daemon`, `submit`, `get`, `list`, `wait`, `cancel`, `status` raw JSON, `attach`/`finish`/`release`), `history.py` (insert only), `status.py` (raw views), `tests/daemon/*`, `tests/live/test_shim_live.py`.

**Do:** 05 completely. 07 §1–10. Startup reconciliation. Observe mode with a simple `would` (PR 4 makes it exact). `cancel` per 05 §5, except the cooperative branches, which only set the flag until PR 6 (stub `request_checkpoint` as "not yet supported → flag only"). Memory fit and the GPU/CPU/lock ledger per 06 §5, because the temporary admission needs them.

**Tests:** 05 §7, 07 §10. Must include `test_commit_before_response`, `test_attach_before_gate_release`, `test_two_submits_one_exclusive_holder`, `test_socket_close_after_submit_does_not_cancel`, `test_observe_inserts_nothing`, and `test_live_reconcile_after_daemon_restart`.

**Acceptance:** On a real Mac, `gsched daemon` in one terminal and `gsched submit` of a `python -c` job with `enforce: true` in another: it runs, `get` shows `done`, and `$H/jobs/<id>/stdout.log` has the output. Kill the daemon during a `sleep 20` job, restart it, and the job ends `done`.

## PR 4: Scheduling policy

**Files:** `policy/{model,windows,fit,rank,admit}.py`, `tick.py` (phase 2 usage bookkeeping, phase 7 real admission, dependency cascade), `status.py` (`explain`, `blocked_by`, `position`), `tests/policy/*`.

**Do:** 06 §0–6, §9, §12. 12 §5.

**Tests:** the PR 4 list in 06 §13, plus `test_blocked_by_for_each_code`.

## PR 5: Maintenance, morning edge, preemption, pressure, protective

**Files:** `policy/{maintenance,morning,preempt,pressure}.py`, `tick.py` (phases 3–6), `scan.py` (protective state), `tests/policy/*`, `tests/daemon/test_signals.py`.

**Do:** 06 §7, §8, §10, §11. Protective actions (07 §11). The `atomic_never_signaled` property test (13 §3). Cooperative behaviors in this PR rely on `request_checkpoint` / `mark_spilled` from 04. Implement them here (the signal plus state). PR 6 adds the READY handling.

**Tests:** the PR 5 list in 06 §13, protective tests from 07 §11, `test_atomic_never_signaled`, `test_cooperative_checkpoint_at_0800_not_killed_on_ignore`, `test_higher_priority_day_job_waits_during_atomic_spill`.

## PR 6: Cooperative checkpoints and residents

**Files:** `residents.py`, `projects.py` (profiles), `supervise.py` (READY finalize), `examples/checkpoint_counter.py`, `tests/residents/*`, `tests/live/test_counter_live.py`.

**Do:** 08 completely. Replace the PR 3 cancel stubs.

**Tests:** 08 §9.

## PR 7: Full CLI, history, suggest

**Files:** `cli.py` (all commands in 09), `history.py` (`suggest`, submit warnings), `tests/cli/*`.

**Do:** 09 completely. 12 §6. `gsched run`. `logs --follow`. TTY tables. `config get/set`. `project add/list`. `gc`.

**Tests:** 09 §6, `test_suggest_p95`, `test_submit_warns_low_memory_vs_history`.

## PR 8: MCP adapter and AGENTS.md

**Files:** `mcp_stdio.py`, `AGENTS.md`, `spec.py` (`spec_json_schema()`), `tests/mcp/*`.

**Do:** 10 completely.

**Tests:** 10 §8.

## PR 9: Install, doctor, README, examples

**Files:** `install.py`, `doctor.py`, `deploy/com.gpuscheduler.agent.plist.template`, `README.md`, `examples/**` (jobs, projects, profiles with `${LLM_ROOT}` placeholders, config presets, `adapters/README.md`), `tests/package/*`.

**Do:** 11 §1–5. `examples/adapters/README.md` explains: (a) an external launcher using `gsched.launcher.admitted`, with `spawn: external` and `checkpoint: atomic`, which waits for its own preconditions **before** `wait --until start` so it doesn't hold capacity while waiting; (b) a model-server resident profile; (c) converting decimal-GB constants to GiB. No private paths, no private repo names beyond generic wording.

**Tests:** `test_install_refuses_python39` (fake interpreter script printing `(3, 9) arm64`), `test_install_copies_not_symlink`, `test_install_idempotent`, `test_install_never_overwrites_config`, `test_plist_template_has_no_personal_paths`, `test_plist_renders_valid_plist`, `test_default_home`, `test_doctor_reports_tcc_warning` (fake listdir PermissionError), `test_readme_names_modes_queues_and_platform`, `test_example_specs_validate`.

**Acceptance:** On a clean user account (or a fresh `GSCHED_HOME`): the README's install steps work verbatim. `doctor` gives correct TCC advice when the daemon lacks Full Disk Access.

## PR 10: Publishing hygiene, CI, v0.1.0

**Files:** `.github/workflows/ci.yml`, `tests/package/test_tree_has_no_personal_data.py`, `CHANGELOG.md`.

**Do:** 11 §6. Run the manual acceptance list in 13 §5 on the reference Mac and record the results in the PR description.

---

## Hand-off notes for the implementer

- Read `DESIGN.md`, then this folder in order. Then start PR 1.
- Keep `docs/implementation/QUESTIONS.md` with anything ambiguous. Don't silently pick a policy.
- When a test name in these docs and the behavior disagree, the **behavior described in the section text** wins. Fix the test name.
- The owner's machine-specific integrations (latent-alpha launcher, local-llm resident, plist handover) are **out of scope** for these PRs. They're described in `docs/local/this-machine.md` and become private follow-up work once v0.1.0 runs on that machine in observe mode.
