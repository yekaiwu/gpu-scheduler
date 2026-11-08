# 09: CLI

`gsched/cli.py` uses `argparse` with subcommands. The client side imports only `gsched.client`, `gsched.spec` (for `run`), and the stdlib. `gsched mcp` imports `gsched.mcp_stdio` inside its handler. `gsched daemon` imports `gsched.daemon` inside its handler. Keep `gsched --help` fast (no heavy imports at module top).

## 1. Conventions

- JSON results go to **stdout**, one object (pretty-printed when stdout is a TTY, compact otherwise; `--json` forces compact). Human messages go to **stderr**.
- `--home PATH` overrides `GSCHED_HOME`. `--socket PATH` overrides the socket path.
- Durations accept `90`, `90s`, `15m`, `4h`, `1d`, and `1h30m`. Memory accepts `20`, `20G`, `20GiB` (all mean GiB) and `512M` / `512MiB`. Writing `GB` is rejected with a hint to use GiB.
- Exit codes: 0 ok; 2 refused / cancelled / ended not-done; 3 can't connect; 4 request error; 64 usage; 78 config/platform (daemon only); 75 another daemon already running.
- Connect: `ENOENT`/`ECONNREFUSED` → exit 3 with "gsched daemon is not running (socket: …). Start it with `gsched install` or `gsched daemon`." `wait` is the exception: it retries for 30 s first (07 §6).

## 2. Commands

| Command | Does | Output / exit |
|---|---|---|
| `gsched daemon [--foreground]` | Run the daemon (05 §1) | Runs until SIGTERM |
| `gsched submit --file F \| -` `[--after ID]... [--priority N] [--queue Q] [--print-id]` | Read the spec from a file or stdin. Flags override spec fields. Fill `cwd` with the caller's cwd if absent | Result JSON. `--print-id` prints only the id. 0 / 2 / 4 |
| `gsched run [opts] -- argv...` | Build a spec from flags and submit it | Same as submit |
| `gsched exec [opts] -- argv...` | Fail-open wrapper: run an existing command as an external job (§3b) | The command's exact status; 75 / 143 / 130 / 129 / 2 / 64 as in §3b |
| `gsched get ID [--wait S]` | Job view | 0 (2 for unknown) |
| `gsched list [--state S]... [--project P] [--limit N] [--all]` | Defaults to non-terminal only. `--all` includes terminal | Table on a TTY, JSON otherwise |
| `gsched wait ID [--until exit\|start] [--timeout DUR]` | Park until the condition | `exit`: 0 if `done`, 2 if another terminal state, 124 on timeout. `start`: prints the lease JSON, 0 |
| `gsched cancel ID [--terminate]` / `gsched cancel --lease L` | | Result JSON |
| `gsched estimate ID DUR` | Set `estimate_s` | |
| `gsched logs ID [--stderr] [--tail BYTES] [--follow]` | Print the log. `--follow` polls `logs` with `offset` every 0.5 s until the job is terminal and EOF is reached | Raw text to stdout |
| `gsched status [--brief]` | 12 §3. `--brief` = capacity one-liner | |
| `gsched ensure PROFILE` | 08 §3 | View JSON |
| `gsched inhibit\|uninhibit\|hold\|unhold PROFILE` | | |
| `gsched drain` / `gsched undrain` | | |
| `gsched attach --lease L --pid P` / `finish --lease L --exit-code N` / `release --lease L` | External launchers | |
| `gsched suggest --project P [--name N]` | History-based resource suggestion | |
| `gsched project add NAME [--weight W] [--mcp-submit] [--default-cwd D]` | Write `$H/projects/NAME.json` (0600). Refuses to overwrite without `--force`. Sends `reload` if the daemon is up | |
| `gsched project list` | List project files | |
| `gsched config get [KEY]` / `config set KEY VALUE` | Edit `$H/config.json` atomically (JSON-parse VALUE, falling back to a string). Validates with the same `Config` loader before writing. Sends `reload` | |
| `gsched install [--python P] [--home H] [--no-load] [--venv-symlinks]` | 11 §2 | |
| `gsched uninstall [--keep-data]` | 11 §3 | |
| `gsched doctor [--json]` | 11 §4 | 0 if no failures, 2 otherwise |
| `gsched gc [--older-than DUR]` | Retention now | |
| `gsched mcp` | MCP stdio server (10) | |
| `gsched version` | Version + daemon `ping` if reachable | |

## 3. `gsched run`

```
gsched run [--project P] [--name N] [--mem GIB] [--gpu exclusive|shared|none] [--cpu N]
           [--lock L]... [--est DUR] [--limit DUR] [--checkpoint atomic|cooperative|restartable]
           [--priority N] [--queue auto|day|night] [--after ID]... [--allow-spill]
           [--env K=V]... [--cwd DIR] [--wait] [--follow] -- argv...
```

- `--project` defaults to `$GSCHED_PROJECT`, then `default_project` from config (an optional key; add it to 01 §7 as `default_project: null`). If neither is set → usage error 64.
- `--mem` is **required**: no guessing. If missing, print the `suggest` result for `(project, name)` when there's history, and exit 64.
- `--checkpoint` is **required**, for the same reason as the spec.
- `--wait`: after submit, behave like `gsched wait ID` (exit code included). `--follow`: stream stdout logs until the job is terminal, then exit with the wait code. Both run locally and cost no tokens.
- Prints the submit result JSON (or only the logs with `--follow`).

Example for an agent:

```sh
gsched run --project latent-alpha --name screen-p91 --mem 20 --gpu exclusive \
  --checkpoint atomic --est 3h -- python pipelines/p91.py --stage full
```

## 3b. `gsched exec` (16 §1)

```
gsched exec [--project P] [--name N] --mem GIB [--gpu M] [--cpu N] [--lock L]... --checkpoint MODE
            [--est DUR] [--limit DUR] [--priority N] [--queue auto|day|night]
            [--max-wait DUR] [--on-max-wait run|fail] [--fail-open | --fail-closed] -- argv...
```

Meant for the front of an existing LaunchAgent's `ProgramArguments` or a cron line. `--mem` and `--checkpoint` are required (usage error 64); `--project` falls back to `$GSCHED_PROJECT`, then `default_project`; `--name` defaults to `basename(argv[0])`. An atomic/cooperative command without `--est` is submitted with `default_unknown_estimate_s` (a wrapper has no human to run `gsched estimate`). The spec is `spawn: external` with `cwd = os.getcwd()`, the argv as `command`, and no `env` (the child inherits ours).

| Situation | Fail-open (default) | `--fail-closed` |
|---|---|---|
| Daemon unreachable, `state: observe`, no job queued | `execvp(argv)`; stderr `gsched: exec fallback: <reason>` | exit 75 |
| Request error (bad spec, unknown project, `too_big`, `draining`) | `execvp(argv)` after the loud stderr line | exit 2 |
| Daemon lost while queued for more than 30 s | `execvp(argv)` | exit 75 |
| Job ended (cancelled by someone) before admission | exit 2, command not run | exit 2 |
| SIGTERM / SIGINT / SIGHUP while queued | `cancel`, exit 143 / 130 / 129, command not run | same |
| `--max-wait` expired | `cancel` with `end_reason=max_wait`, then `--on-max-wait run` (default): `execvp(argv)`; `fail`: exit 75 | `fail` is the default |

Once admitted, the wrapper starts the command with `gsched.launcher.Lease.spawn(new_session=False)`: a gated child in the wrapper's own session and process group, stdio inherited, then `attach`. SIGTERM/SIGINT/SIGHUP/SIGUSR1/SIGUSR2/SIGQUIT are forwarded to it (`platform.darwin.signal_own_child`; a signal that arrives before the child exists is delivered right after the gate opens). The wrapper exits with the child's exact status, or dies of the same signal (`exit_by_signal`), after `finish` (3 tries; a failure is reported on stderr and the daemon will see the pid gone and mark the job `lost`). Signal dispositions that were `SIG_IGN` at start (a `nohup`'d SIGHUP) are left ignored. The wrapper module is imported lazily: `gsched --help` stays light.

## 4. Output tables (TTY only)

`gsched list`:

```
ID                      PROJECT        NAME         STATE   WHY/RUNNING     MEM    GPU   PRI  Q
20261108T231500-7f3a    example-train  sweep-lr     held    live 2h13m      20.0G  excl    0  night
20261108T231700-01bc    example-train  tokenize     queued  memory_ledger   12.0G  none    0  day
```

`gsched status --brief`:

```
48.0 GiB machine · budget 40.0 · reserved 32.0 · live free 9.7 · GPU exclusive(1) · pressure normal · 1 running · 3 queued · enforce on
```

## 5. Client (`gsched/client.py`)

```python
def call(method: str, params: dict, *, door="cli", timeout: float | None = 10.0, home=None) -> dict:
    """Connect, send one JSON line, read one JSON line. Raises ConnectError (exit 3) or returns the envelope.
    timeout=None for wait/get --wait."""
```

The CLI and the MCP adapter both use this. It's the only socket client code.

## 6. Tests (PR 7)

`test_cli_exit_codes` (table-driven against a fake daemon socket), `test_cli_submit_fills_cwd`, `test_cli_run_requires_mem_and_checkpoint`, `test_cli_rejects_gb_unit`, `test_cli_duration_parsing`, `test_cli_connect_failure_exit_3`, `test_cli_wait_retries_then_3`, `test_cli_wait_timeout_124`, `test_cli_json_when_not_tty`, `test_cli_help_does_not_import_daemon_or_mcp`, `test_config_set_validates_before_write`, `test_project_add_mode_0600`.
