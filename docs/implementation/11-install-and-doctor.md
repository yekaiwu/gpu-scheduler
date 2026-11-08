# 11: Install, doctor, packaging

## 1. Supported ways to get the code

| Method | What it gives |
|---|---|
| `git clone` + `python3.12 -m gsched install` from the checkout | Recommended. Install copies everything it needs into `$GSCHED_HOME` |
| `uv tool install gpu-scheduler` / `pipx install gpu-scheduler` | The CLI on PATH. Then `gsched install` sets up the daemon from the installed package |

Either way, the **LaunchAgent never runs code from the checkout** (which may be under `~/Documents`, protected by TCC) or from a tool venv that `uv`/`pipx` might replace.

## 2. `gsched install`

```
gsched install [--python /abs/python3.12] [--home ~/.local/gpu-scheduler] [--no-load] [--force] [--venv-symlinks]
```

Steps, each idempotent:

1. **Interpreter.** `--python`, or else the current `sys.executable`. Run `[py, "-c", "import sys,platform; print(sys.version_info[:2], platform.machine())"]`. Refuse below 3.12 with a message that names the path and version. Example: "/usr/bin/python3 is 3.9.6 (macOS system Python). Install 3.12+: `brew install python@3.12` or `uv python install 3.12`." Refuse non-arm64 (Rosetta).
2. **Home.** Create the `$H` subdirectories (01 §6) with modes.
3. **Venv.** `[py, "-m", "venv", "--copies", "--without-pip", f"{H}/venv"]`. `--copies` gives a stable binary path to grant Full Disk Access to. Verify that `"$H/venv/bin/python3" -c "import sys; print(sys.version)"` works. `--venv-symlinks` (16 §4) creates the venv with symlinks instead, so the daemon runs the **same interpreter binary** as `--python`; use it when that binary already has Full Disk Access (or works from LaunchAgents under `~/Documents`). Install prints the realpath to grant and `doctor` check 18 repeats it; switching between the two modes rebuilds the venv. If `--copies` fails on a given Python build, fall back to symlinks and print a warning: Full Disk Access must then be granted to `os.path.realpath(<venv python>)`, and must be re-granted after Python upgrades.
4. **App copy.** Copy the `gsched` package directory (located via `gsched.__file__`) to `$H/app/gsched/`, using a fresh temp dir and then an atomic rename (`$H/app.new` → `$H/app`; the old one moves to `$H/app.old` and is deleted after a successful load). Write `$H/venv/lib/python3.X/site-packages/gsched-app.pth` containing the single line `$H/app`. Don't symlink. `test_install_copies_not_symlink`.
5. **Wrapper.** `$H/bin/gsched`:
   ```sh
   #!/bin/sh
   exec "/Users/<you>/.local/gpu-scheduler/venv/bin/python3" -I -m gsched "$@"
   ```
   (Always render the absolute venv path at install time. The template in the package uses a `{{VENV_PYTHON}}` placeholder, never a literal home.) `-I` keeps user site-packages and `PYTHON*` env vars out of the daemon. The `.pth` in the venv's own site-packages is still honored under `-I`. Verify this on the machine as part of install: run `$H/bin/gsched version`. Print: "Add to PATH: `export PATH=\"$H/bin:$PATH\"`, or `ln -s $H/bin/gsched ~/.local/bin/gsched`".
6. **Config.** If `$H/config.json` is absent, write `{"enforce": false}` plus a `"_comment"` key (ignored by the loader) pointing at the docs. Never overwrite an existing config.
7. **Example project.** If `$H/projects/` is empty, write `default.json` (`{"name":"default","weight":1}`) and set `default_project: "default"` in the new config only.
8. **LaunchAgent.** Render the template to `~/Library/LaunchAgents/<label>.plist` (0644). If one exists and differs, back it up to `$H/rollback/<label>.plist.<ts>`.
9. **Load** (unless `--no-load`): `launchctl bootout gui/<uid>/<label>` (ignore errors), then `launchctl bootstrap gui/<uid> <plist>`, then wait up to 10 s for `ping`. Print `status --brief`.
10. Print next steps: run `gsched doctor`, Full Disk Access if needed, add the MCP snippet, `gsched config set enforce true` when ready.

Never start a resident or enable enforcement as a side effect.

### Plist template (`deploy/com.gpuscheduler.agent.plist.template`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{{LABEL}}</string>
  <key>ProgramArguments</key><array>
    <string>{{VENV_PYTHON}}</string><string>-I</string><string>-m</string><string>gsched</string><string>daemon</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GSCHED_HOME</key><string>{{HOME_DIR}}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>{{HOME_DIR}}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{{HOME_DIR}}/logs/launchd.err.log</string>
</dict></plist>
```

- `KeepAlive.SuccessfulExit=false`: restart after a crash, but not after a clean exit (exit 0). Exit 78 (config) is non-zero, so launchd retries every 15 s. That's acceptable: the log says why.
- `ProcessType Standard`, not `Background`. Background agents get throttled CPU and I/O, and their children inherit the throttling. That would slow every job.
- Render with `str.replace` on `{{…}}`, then XML-escape the values (`xml.sax.saxutils.escape`). Validate with `plistlib.loads` before writing.
- The template contains no `/Users/` literal (`test_plist_template_has_no_personal_paths`).

## 3. `gsched uninstall [--keep-data]`

`launchctl bootout`, then remove the plist. Without `--keep-data`, **refuse** if any job is `held` (from `status`), with the message "jobs are running; drain first or pass --force". With `--force` or no running jobs: delete `$H/venv`, `$H/app`, and `$H/bin`. Delete the state, logs, jobs, and checkpoints only with `--purge`, after printing their sizes.

## 4. `gsched doctor`

Runs two groups. **Local** checks run in the CLI process. **Daemon** checks are run via the `doctor` RPC **inside the daemon process**, because TCC applies per process and only the daemon's view matters for jobs. Output: one line per check, `PASS`/`WARN`/`FAIL` plus a fix. `--json` gives a structured list. Exit 2 if any FAIL.

| # | Check | Where | FAIL/WARN → fix text |
|---|---|---|---|
| 1 | Apple silicon, native arm64 Python ≥ 3.12, macOS ≥ 14 | local | names what's wrong |
| 2 | Daemon reachable (`ping`) | local | "not running: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<label>.plist`, see `$H/logs/launchd.err.log`" |
| 3 | Daemon version == CLI version | local | WARN: "re-run `gsched install` to update the daemon copy" |
| 4 | Layout self-check passed | daemon | FAIL: report it (`tools/check_layouts.c`) |
| 5 | Memory probe sane: reclaimable vs a fresh `vm_stat` within 5 % | daemon | FAIL |
| 6 | Swap readable, `memorystatus_level` readable | daemon | FAIL/WARN |
| 7 | **TCC:** the access state of `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Library/Mobile Documents` and each `tcc_protected_roots` entry **as seen by the daemon**, taken from the daemon's probe cache (`ok` / `blocked` / `denied` / `absent`; `unknown` → INFO and a probe is started). The doctor RPC **never lists a protected folder in-process**: in the daemon that call would block behind a consent prompt (03 §5 step 6) | daemon | WARN per folder: "The daemon cannot read ~/Documents. Jobs whose cwd or files are there will fail. Fix: System Settings → Privacy & Security → Full Disk Access → + → press ⌘⇧G and paste `<realpath of daemon sys.executable>` → enable → `launchctl kickstart -k gui/$UID/<label>`. Alternative: keep job code outside protected folders." |
| 8 | Each project's `default_cwd` readable by the daemon (inside a protected root: the cached probe state, never a `listdir`) | daemon | WARN |
| 9 | Each profile loads (env expansion, binary exists, executable) | daemon | FAIL per profile |
| 10 | `iogpu.wired_limit_mb` | daemon | INFO: "0 = macOS default (about 2/3–3/4 of RAM can be GPU-wired). Large models may need `sudo sysctl iogpu.wired_limit_mb=<N>` (not persistent across reboot). gsched never changes it." If any profile/job `memory_gib` with `gpu != none` exceeds 75 % of memsize → WARN |
| 11 | Budget: memsize, `os_reserve`, budget, `headroom` | daemon | INFO |
| 12 | Foreign heavy processes right now | daemon | INFO: lists them (protective mode on or off) |
| 13 | `enforce` | daemon | INFO: "observe mode: nothing is scheduled. `gsched config set enforce true`" |
| 14 | Socket path length and modes (dir 0700, socket 0600, db 0600) | local | FAIL |
| 15 | `caffeinate` present | daemon | WARN |
| 16 | Low Power Mode (`pmset -g` → `lowpowermode 1`) | daemon | WARN: "jobs will run slower" |
| 17 | PATH: `gsched` on PATH matches `$H/bin/gsched` or the same version | local | WARN |
| 18 | Daemon interpreter: `sys.executable`, and its realpath when it is a symlink (`--venv-symlinks`), i.e. the binary that needs Full Disk Access | daemon | INFO |

## 5. README outline (PR 9)

1. One-paragraph pitch: memory-packing scheduler for Apple silicon. Humans and agents submit jobs. No LLM inside.
2. Why: unified memory, no GPU freeze, agents starting duplicate model servers.
3. Install (5 commands) + `doctor` + the Full Disk Access note.
4. 60-second tour: `gsched run … --wait`, `status --brief`, `logs`, `get` with `blocked_by`.
5. Concepts: one table each for resources, checkpoint modes, and queues.
6. For agents: the MCP snippet, plus "copy AGENTS.md into your repo".
7. Residents: a llama-server profile example (placeholders).
8. Config reference link. Presets `workstation` vs `dedicated`.
9. Safety guarantees (the ground rules, in plain words).
10. Limits: Apple silicon only, single user, no sandbox, self-reported doors.
11. Contributing + license.

`test_readme_names_modes_queues_and_platform` checks for the strings: `restartable`, `cooperative`, `atomic`, `night`, `Apple silicon`, `Full Disk Access`, `gsched mcp`.

## 6. Publishing checklist (PR 10)

- `test_tree_has_no_personal_data` scans every tracked file except `docs/local/**` for: `/Users/` followed by a name other than `me`/`you`/`USER`, `100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+` (CGNAT/Tailscale), `api-keys`, and private-key headers. Owner-specific strings (username, label prefix, hostnames) are read from an optional, gitignored `docs/local/private-patterns.txt` (one regex per line), so the public test never spells them out.
- `git check-ignore docs/local/this-machine.md` succeeds.
- GitHub Actions: `macos-14` (arm64) runner: `python3.12 -m pytest -q` and `pytest -m live`. Also an `ubuntu-latest` job running only the pure tests (`tests/policy`, `tests/spec`), to keep the policy free of platform code.
- Tag `v0.1.0`. Release notes say "beta: observe-mode by default".
