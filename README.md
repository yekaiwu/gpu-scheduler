# gpu-scheduler

A memory-packing job scheduler for **Apple silicon** Macs. Humans, shell scripts and LLM agents describe a job once, in
one JSON document, and a small local daemon decides **when** it may start: several jobs run at once if their memory
reservations fit the budget and the live memory sample agrees, two GPU-heavy jobs never fight over the GPU, and a
24-hour training run does not land on the afternoon you wanted for interactive work. There is no LLM inside the
scheduler: it is deterministic policy you can test with a fake clock.

Stdlib-only Python 3.12+. No third-party runtime dependencies.

## Why

Apple silicon has one unified memory pool shared by CPU, GPU and OS. There is no VRAM to partition and no way to freeze
a Metal job and resume it later, so two jobs that each "fit" can push the machine into swap, and the OS answers real
memory pressure by killing processes. Meanwhile coding agents keep starting duplicate model servers because they cannot
see each other. `gpu-scheduler` is admission control for that machine: it counts memory, arbitrates the GPU, and only
stops a job in the ways that job has said it can be stopped.

## Install

You need a native arm64 Python 3.12+ on macOS 14 or newer (Rosetta is refused).

```sh
git clone https://github.com/yekaiwu/gpu-scheduler && cd gpu-scheduler
python3.12 -m gsched install            # private venv + LaunchAgent under ~/.local/gpu-scheduler
export PATH="$HOME/.local/gpu-scheduler/bin:$PATH"
gsched doctor
gsched config set enforce true          # only when you are ready: the install is in observe mode
```

`gsched install --venv-symlinks` makes the venv from symlinks, so the daemon runs the **same interpreter binary** you
passed with `--python` (use it when that binary already has Full Disk Access); `gsched doctor` prints the realpath to grant.

`gsched install` never changes anything about scheduling. It writes `enforce: false`, so `submit` only tells you what
*would* happen ("observe mode") until you enable enforcement.

**Full Disk Access.** The LaunchAgent is subject to macOS privacy controls (TCC). Jobs spawned by the daemon inherit
them, so a job whose working directory is under `~/Documents`, `~/Desktop` or `~/Downloads` fails unless the daemon's
Python has **Full Disk Access**. `gsched doctor` checks this from inside the daemon and prints the exact interpreter
path to add (System Settings > Privacy & Security > Full Disk Access). Submit also refuses such a job up front with
`cwd_not_accessible` and the same hint. Keeping job code outside those folders avoids the issue entirely.

Without that approval a launchd-started process does not get an error from `open()` or `chdir()` in those folders: it
**blocks** behind a consent prompt nobody can answer. The single-threaded daemon therefore never touches such a path
itself. It learns whether it may read each protected folder from a short-lived probe subprocess (killed after 3 s, re-run
every 10 minutes), and the job's working directory is entered by the job's own process, not by the daemon. Extra
protected locations can be listed in the `tcc_protected_roots` config key.

## A 60-second tour

```sh
gsched run --project default --name hello --mem 2 --gpu none --checkpoint atomic --est 1m --wait -- python3 -c "print('hi')"
gsched logs <JOB_ID>
gsched status --brief
# 48.0 GiB machine · budget 40.0 · reserved 0.0 · live free 21.0 · GPU idle · pressure normal · 0 running · 0 queued · enforce on
gsched get <JOB_ID>          # state, exit info, and blocked_by + why for a waiting job
```

`--mem` and `--checkpoint` are required: the scheduler never guesses. `gsched suggest --project P --name N` proposes a
memory size from past runs. Waiting (`gsched wait`, `--wait`, `logs --follow`) happens in a local process and costs no
tokens.

## Concepts

**Resources.** Memory is a reservation in **GiB** (`2**30` bytes) everywhere in the public API.

| Resource | Capacity | Notes |
|---|---|---|
| Memory | budget = RAM minus an OS reserve (default `max(8 GiB, 12.5 %)`), and the live sample must agree | A job counts as `max(reservation, footprint)` |
| GPU | one `exclusive` job, or up to `gpu_shared_max` `shared` jobs; `none` runs beside either | Default is `exclusive` |
| CPU | `cpu_budget` cores (default: physical cores) | A soft limit |
| Locks | named strings, one holder each | Profiles add `port:<n>` automatically |

**Checkpoint modes.** Declared by the job, never guessed. The daemon only stops a job in the way its mode allows, and
never signals a process it did not spawn.

| Mode | Meaning | The daemon may | The daemon must not |
|---|---|---|---|
| `restartable` | can be stopped and restarted from the same argv | `SIGTERM` the process group, `SIGKILL` after 15 s, requeue | call that a checkpoint |
| `cooperative` | saves on `SIGUSR1`: writes `$GSCHED_CHECKPOINT_DIR`, then `READY`, then exits 0; resumes on `GSCHED_RESUME=1` | send one `SIGUSR1` per request, requeue the same job id | `SIGTERM`/`SIGKILL` it to get a checkpoint |
| `atomic` | cannot checkpoint; runs to the end (the safe default) | nothing, once started | send any signal on its own initiative |

`examples/checkpoint_counter.py` is a reference cooperative worker.

**Queues.** A `day` job is eligible at any hour. A `night` job (23:00-08:00 local by default, `queue: auto` sends
anything estimated over 2 h there) waits for the window. An `atomic` night job that cannot finish before the window ends
does not start (`blocked_by: fit_window`). At the morning edge, once per civil day, cooperative night jobs get one
checkpoint request. `examples/config/dedicated.json` turns the window off for a box that only runs jobs.

**Time reservations.** A config entry can hold memory (and optionally the GPU) for one project during a weekly window,
so a nightly pass is not starved by whatever else was running. Other projects' jobs are held back while the window is
active, and also before it when their estimate says they would still be running when it opens
(`blocked_by: reservation_window:<name>`). The owner project is unaffected. `unload_restartable` additionally unloads
restartable jobs of other projects once, at the window start. `status` and the dashboard show active and upcoming
windows. See `docs/implementation/16-exec-reservations-hardening.md`.

```json
"reservations": [{"name": "nightly-pass", "days": ["mon","tue","wed","thu","fri"], "start": "19:00", "end": "23:30",
                  "project": "example-train", "memory_gib": 27, "gpu": "exclusive", "unload_restartable": true}]
```

## Wrapping an existing job: `gsched exec`

To put an existing LaunchAgent or cron job under the scheduler without editing the program, put `gsched exec ... --` in
front of its command (in the plist's `ProgramArguments`, or on the cron line):

```sh
gsched exec --project example-train --name nightly-eval --mem 20 --gpu exclusive --checkpoint atomic --est 3h \
  --max-wait 6h -- python3 pipelines/eval.py --stage full
```

The wrapper submits an external job, waits (locally, holding one socket) until the daemon admits it, runs your command as
its direct child in the **same process group and session** as before (so `launchctl bootout` signals reach it as
usual), and exits with the command's **exact** status, re-raising its signal if it died of one. It is **fail-open**: if
the daemon is down, in observe mode, or refuses the request, your command is `exec`'d directly (a `gsched: exec fallback:`
line on stderr says why). `--fail-closed` exits 75 instead. SIGTERM, SIGINT or SIGHUP while queued cancels the job and
exits 143/130/129 without running the command; `--max-wait DUR` gives up on capacity and either runs it directly
(`--on-max-wait run`, the default when fail-open) or exits 75 (`fail`). The daemon never signals a wrapped command.

## For agents

Add the MCP server (a stdio proxy; it holds no state and makes no decisions):

```json
{ "mcpServers": { "gpu-scheduler": { "command": "gsched", "args": ["mcp"] } } }
```

Claude Code: `claude mcp add gpu-scheduler -- gsched mcp` (use `~/.local/gpu-scheduler/bin/gsched` if `gsched` is not on
the client's PATH). Tools: `submit_job`, `get_job` (with `wait_s` up to 30), `list_jobs`, `cancel_job`, `get_job_logs`,
`get_status`. MCP submission is off per project until its project file says `"mcp_submit": true`, because MCP otherwise
lets an agent with no shell run arbitrary commands.

Copy [`AGENTS.md`](AGENTS.md) into the repos your agents work in.

## Residents

A model server is a `restartable` job that stays up. Describe it as a profile in `$GSCHED_HOME/profiles/` and call
`gsched ensure NAME`; it never blocks while the model loads.

```json
{
  "name": "llama",
  "project": "example-train",
  "spec": {"command": ["${LLM_ROOT}/start-llama.sh"], "cwd": "${LLM_ROOT}",
           "resources": {"memory_gib": 27, "gpu": "exclusive"}, "priority": 10},
  "listen_port": 8081,
  "health_url": "http://127.0.0.1:8081/health",
  "slots_url": "http://127.0.0.1:8081/slots",
  "idle_unload_s": 1800
}
```

Machine-specific values go in `$GSCHED_HOME/env` (`LLM_ROOT=/path/to/models`). Leave `preempt_restartable` at `false`
until you have rehearsed a handover.

## Dashboard

`gsched dashboard` is an opt-in, **read-only** web view (capacity, running jobs, the queue with reasons, recent results,
a memory timeline). It is a separate process bound to loopback, protected by a token file, and it can only call the
daemon's read-only methods. The daemon itself opens no TCP port.

```sh
gsched dashboard --print-url        # http://127.0.0.1:8765/?t=<token>
gsched dashboard --open
gsched install --dashboard          # also run it as a LaunchAgent
```

## Configuration

Every key, with its default and validation rule, is in
[`docs/implementation/01-repo-and-conventions.md`](docs/implementation/01-repo-and-conventions.md#7-config-reference).
`gsched config get` shows the effective config and `gsched config set KEY VALUE` validates before writing. Presets:
`examples/config/workstation.json` (night window on, no backfill) and `examples/config/dedicated.json` (no night window).

## Closing desktop apps for heavy work: `yield_apps`

Opt-in. Name an app bundle and say when the scheduler may quit it; it reopens the app (in the background, with
`open -g`) once the memory is no longer needed:

```json
"yield_apps": [{
  "name": "terminal", "app": "/Applications/Some Data Terminal.app",
  "reservations": ["evening-pass"], "for_jobs_gib": 16,
  "busy_ports": [9001], "quit": "applescript", "reopen_delay_s": 60
}]
```

- `reservations`: closed while one of these reservation windows is active.
- `for_jobs_gib`: or when a queued job of at least this many GiB has been blocked only by live memory for `job_wait_s`
  (60 s) and closing the app would let it fit.
- `busy_ports`: never quit while another process is connected to the app's local API (a data client mid-download).
- `quit`: `applescript` (default) or `sigterm` (graceful for Electron/Chromium apps only).

It quits gracefully only and never force-kills: if the app is still open after `quit_timeout_s` (a save dialog), it is
left alone. If you reopen the app yourself, the scheduler does not close it again for that episode. It reopens only apps
it closed. `gsched status` and the dashboard show a closed app. The `applescript` method needs a one-time Automation
consent for the daemon's Python; `sigterm` needs none.

## Safety guarantees

In plain words, and enforced by tests:

- It never signals a process it did not start. Foreign processes, IDEs, browsers and launchd services have no signal path
  in the code. A foreign `llama-server` makes the scheduler stop admitting new GPU work; it is never touched. The one
  exception is opt-in: an app you list in `yield_apps` gets a graceful quit (never SIGKILL) and is reopened afterwards.
- It never signals an `atomic` job, or a cooperative job that failed to checkpoint, on its own initiative. Only an explicit
  `gsched cancel --terminate` by you can stop one.
- It commits to SQLite before it replies to a client or lets a job start.
- When a probe fails (swap unreadable, layout check, identity), it admits nothing new and signals nothing.
- Observe mode changes nothing: no row is inserted, nothing is spawned, nothing is signaled.

## Limits

Apple silicon only. Single user: the socket is `0600` and peers must have the daemon's uid. There is no sandbox: the
daemon refuses to oversubscribe, it does not contain anything. The "door" a client reports (`cli`, `mcp`) is
self-reported, so it is a policy hint, not a security boundary; the per-project MCP opt-in protects against an agent that
only has MCP tools. `caffeinate` keeps the Mac awake for running jobs but cannot stop a laptop sleeping when the lid
closes on battery.

## Contributing and license

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`DESIGN.md`](DESIGN.md). Licensed under the Apache License 2.0
([`LICENSE`](LICENSE)). The build instructions in `docs/implementation/` are normative; judgment calls are recorded in
`docs/implementation/QUESTIONS.md`.
