# Adapters: integrating a project with gpu-scheduler

Four common shapes. The first, `gsched exec`, is the recommended way to wrap an existing LaunchAgent. None of them needs anything from this repository except the `gsched` CLI (or the stdlib-only
`gsched.launcher` helper).

## (0) Wrapping an existing LaunchAgent or cron job: `gsched exec` (recommended)

If a LaunchAgent already runs a pipeline and you do not want to edit the program, put the wrapper in front of its
`ProgramArguments`. Nothing else changes: same working directory, same environment, same stdout/stderr destinations, same
process group, same exit status (so `KeepAlive`/`SuccessfulExit` keep working).

```xml
<key>ProgramArguments</key>
<array>
  <string>/Users/you/.local/gpu-scheduler/bin/gsched</string>
  <string>exec</string><string>--project</string><string>example-train</string>
  <string>--name</string><string>nightly-eval</string>
  <string>--mem</string><string>20</string><string>--gpu</string><string>exclusive</string>
  <string>--checkpoint</string><string>atomic</string><string>--est</string><string>3h</string>
  <string>--max-wait</string><string>6h</string>
  <string>--</string>
  <string>/usr/bin/python3</string><string>/path/to/repo/eval.py</string>
</array>
```

The same works on a cron line: `0 19 * * 1-5 gsched exec --project ... -- ./run.sh`.

- **Fail-open.** If the daemon is not running, is in observe mode, or rejects the job, the command is `exec`'d directly
  (stderr shows `gsched: exec fallback: <reason>`). Use `--fail-closed` to exit 75 instead of running unscheduled.
- **Waiting.** While queued the wrapper holds one local socket and costs nothing. `launchctl bootout` (SIGTERM) cancels
  the queued job and exits 143 without running the command. `--max-wait DUR` stops waiting after that long and then runs
  the command directly (`--on-max-wait run`, default when fail-open) or exits 75 (`--on-max-wait fail`).
- **`--replace` (recommended for LaunchAgents).** After admission the wrapper attaches its own pid and `exec`s the
  command in place: same pid, same ancestry, same macOS privacy (TCC) attribution, and launchd's signals go straight
  to the program. Use it when the program, its venv, or its libraries live under ~/Documents, ~/Desktop or ~/Downloads.
  The exit code isn't reported; the job ends `done` / `exit_unobserved`.
- **Running.** The command is the wrapper's direct child, in the wrapper's session but leading its own process
  group; SIGTERM/INT/HUP/USR1/USR2/QUIT sent to the wrapper (or to its group) are forwarded to it exactly once. The wrapper exits with its exact status (or dies of the same signal), then reports the exit.
- **Memory accounting.** The job's footprint is the command's process tree (descendants of the attached pid), not the
  whole launchd process group.
- **No guessing.** `--mem` and `--checkpoint` are required. An atomic/cooperative job with no `--est` gets the
  `default_unknown_estimate_s` config value. The daemon never signals the wrapped command; `limit_s` only sets
  `yield_requested`, which a cooperative pipeline can read with `gsched get`.
- **Logs.** Output goes wherever launchd or the terminal pointed it. The daemon does not manage or truncate it.

Use (a) below instead only when the program itself must decide when to ask for capacity.

## (a) An external launcher that must own its child process

Use `spawn: external` when your program already has a launcher that starts and supervises the process (a Makefile
target, a Python orchestrator, a job runner). The daemon never starts or signals an external job; it only decides
when it may start and records how it ended.

Wait for your **own** preconditions (inputs downloaded, a checkout ready, a license server reachable) **before** asking
for capacity. Once `wait --until start` returns, you hold memory and the GPU, so do not sit on them.

```python
import gsched.launcher as gl

spec = {
    "project": "example-train",
    "name": "nightly-eval",
    "cwd": "/path/to/repo",
    "resources": {"memory_gib": 20, "gpu": "exclusive", "cpu_cores": 4},
    "runtime": {"estimate_s": 3 * 3600},
    "checkpoint": "atomic",          # the daemon never signals an external job
}

prepare_inputs()                      # your preconditions first

with gl.admitted(spec) as lease:      # submit, then block (locally, no tokens) until admitted
    child = lease.spawn(["python3", "eval.py"], cwd="/path/to/repo")   # gated child -> attach -> release the gate
    code = child.wait()
    lease.finish(code)
```

The same thing from a shell, with the CLI (see `docs/implementation/07-supervision.md` for the gate details):

```sh
id=$(gsched submit --file spec.json --print-id)
gsched wait "$id" --until start          # prints {"lease": "..."} once admitted
# start your child blocked on a gate, then:
gsched attach --lease "$LEASE" --pid "$CHILD_PID"      # only release the gate if this exits 0
gsched finish --lease "$LEASE" --exit-code "$CODE"
```

Always run `gsched release --lease L` in a `finally`/`trap`: it is a no-op on a finished job.

## (b) A model server (resident profile)

A model server is a `restartable` job that stays up. Describe it once as a profile in
`$GSCHED_HOME/profiles/<name>.json` (see `examples/profiles/llama-server.example.json`) and have your tooling call
`gsched ensure <name>` before using it. `ensure` never blocks while the model loads: poll the returned view
(`queued` / `starting` / `running`) or hit the server's own health endpoint.

Put machine-specific paths in `$GSCHED_HOME/env` (`LLM_ROOT=/path/to/models`) and reference them as `${LLM_ROOT}`
in the profile, so the profile itself stays shareable. Leave `preempt_restartable` at `false` until you have rehearsed
a handover on your machine.

## (c) Converting decimal-GB constants to GiB

gpu-scheduler's public API is **GiB** (`2**30` bytes) everywhere. If your code or notes use decimal GB (`10**9`),
convert at your edge:

```python
GIB = 2**30
memory_gib = round(need_gb * 1e9 / GIB, 1)       # 27 GB  -> 25.1 GiB
```

Do not round down: under-asking can push the Mac into swap. `gsched` rejects `--mem 20GB` on purpose.
