# 10: MCP adapter and AGENTS.md

`gsched mcp` is a **separate process** spawned by the agent client (Claude Code, Cursor, and others). It speaks MCP over stdio and forwards to the daemon socket with `door="mcp"`. It holds no state and makes no decisions. The daemon never imports it.

## 1. Transport

- Newline-delimited JSON-RPC 2.0 on stdin/stdout. Each message is one line of compact JSON (`separators=(",",":")`, `ensure_ascii=False`), terminated by `\n`. No `Content-Length` headers. No embedded newlines (compact `json.dumps` guarantees this).
- **stdout carries protocol messages only.** All logging goes to stderr. At startup, set `sys.stdout = sys.stderr` for any stray prints, and write protocol messages via a saved handle: `out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")`.
- Read loop: `for line in sys.stdin.buffer:`. Skip empty lines. Parse errors → JSON-RPC error `-32700` with `id: null`.
- Requests are handled sequentially. A `get_job` with `wait_s` blocks the adapter for up to 30 s. That's acceptable for a per-session stdio server. If the client sends `notifications/cancelled` for an in-flight request, it's read only after the wait returns. Document this limitation.
- EOF on stdin → exit 0.

## 2. Lifecycle

`initialize` request:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"…","version":"…"}}}
```

Response:

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"<negotiated>",
  "capabilities":{"tools":{"listChanged":false}},
  "serverInfo":{"name":"gpu-scheduler","version":"0.1.0"},
  "instructions":"Schedules GPU/CPU/memory-heavy jobs on this Mac. submit_job returns immediately with a job id; poll get_job (use wait_s up to 30) or ask the user to run `gsched wait ID`. Never assume a job ran: check state. Set checkpoint to atomic unless the program handles SIGUSR1 per the cooperative contract."}}
```

Version negotiation: `SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26"]`. If the requested version is in `SUPPORTED`, echo it. Otherwise respond with `SUPPORTED[0]` (the client decides whether to continue). This follows the MCP lifecycle rules.

- `notifications/initialized` (no id) → no response. Tool requests before it are still served: be lenient, since some clients race. Log a warning to stderr.
- `ping` → `{"result":{}}`.
- Unknown method → `-32601`.
- Requests before `initialize` → `-32002` "server not initialized".

## 3. Tools

`tools/list` returns these six tools. Keep the descriptions short and imperative: agents read them. Input schemas are JSON Schema (draft 2020-12 subset). Emit `inputSchema` for all, and `outputSchema` only when the negotiated version is ≥ `2025-06-18`.

### `submit_job`

Description: "Queue a job on this Mac's GPU/memory scheduler. Returns immediately with a job id. Does NOT wait for the job to run. Required: project, command, cwd, checkpoint, resources.memory_gib. Use checkpoint=atomic unless the program implements the SIGUSR1 → READY → exit 0 contract."

`inputSchema`: the job spec (§5 generates it from the `spec.py` field table so the two can't drift). `schema` is optional here: the adapter fills in `gpu-scheduler.job.v1` if it's missing, because agents forget it. Everything else is passed through unchanged.

### `get_job`

Input `{"job": str, "wait_s": int 0..30 (optional)}`. Description: "Get a job's state, why it is waiting (blocked_by/why), and exit info. Pass wait_s (≤30) to wait for a state change instead of polling rapidly."

### `list_jobs`

Input `{"state": str|[str] (optional), "project": str (optional), "limit": int ≤ 100}`.

### `cancel_job`

Input `{"job": str, "terminate": bool (default false)}`. Description: "Cancel a job. Queued jobs are removed. Running atomic jobs are NOT stopped unless terminate=true (unsaved work is lost; may be disabled for the project)."

### `get_job_logs`

Input `{"job": str, "stream": "stdout"|"stderr" (default stdout), "tail_bytes": int ≤ 16384 (default 4096)}`. Returns text.

### `get_status`

Input `{}`. Returns the brief status: machine memory, budget, reserved, live free, GPU occupancy, pressure, protective, enforce, running (id, project, name, mem, gpu, runtime), and queue depth by `blocked_by`.

## 4. `tools/call` results

```json
{"jsonrpc":"2.0","id":7,"result":{
  "content":[{"type":"text","text":"{\"job\":\"20261108T231500-7f3a\",\"state\":\"queued\",…}"}],
  "structuredContent":{"job":"20261108T231500-7f3a","state":"queued",…},
  "isError":false}}
```

- `content[0].text` = compact JSON of the daemon's `result`. For `get_job_logs`, it's the log text itself.
- `structuredContent` only for negotiated version ≥ `2025-06-18`.
- A daemon error envelope → `isError: true`, and the text is `{"error":{"code":…,"message":…}}`. These are tool errors, not JSON-RPC errors, so the agent sees them.
- Daemon unreachable → `isError: true` with "gsched daemon is not running; ask the user to run `gsched install` or `gsched daemon`".
- An unknown tool name or invalid arguments (failing the schema) → JSON-RPC error `-32602`.
- Observe mode: the result includes `state: "observe"`, `job: null`, and the `hint`. The text starts with `"NOT QUEUED (observe mode): "` before the JSON, so an agent can't miss it.
- Never include a lease in any MCP output. `submit_job` with `spawn: external` is rejected by the daemon (`forbidden`).

## 5. Schema generation

`spec.py` holds a single `FIELDS` table (path, type, required, default, enum, min/max, description). Both the validator and `spec_json_schema()` are generated from it. `test_mcp_schema_matches_validator` round-trips: every example in `examples/jobs/*.json` validates under both.

## 6. Client config snippet (README)

```json
{
  "mcpServers": {
    "gpu-scheduler": { "command": "gsched", "args": ["mcp"] }
  }
}
```

Claude Code: `claude mcp add gpu-scheduler -- gsched mcp`. If `gsched` isn't on the client's PATH, use `~/.local/gpu-scheduler/bin/gsched`.

## 7. `AGENTS.md` (repo root; others copy it into their projects)

````markdown
## GPU / memory scheduler (gpu-scheduler)

This Mac runs heavy jobs through `gsched`. Do not start GPU- or memory-heavy
processes directly (training, large inference, model servers): submit them.

Quick path (waiting costs no tokens):

    gsched run --project <project> --name <short-label> --mem <GiB> --gpu exclusive|shared|none \
      --checkpoint atomic --est <duration> -- <command...>
    gsched wait <JOB_ID>            # exit 0 = done; 2 = failed/cancelled
    gsched logs <JOB_ID> --tail 4000

Before submitting, `gsched status --brief` shows free capacity.
`gsched get <JOB_ID>` explains why a job is waiting (`blocked_by`, `why`).
`gsched suggest --project <p> --name <n>` suggests --mem from past runs.

Rules:
- `--mem` is a reservation in GiB. Ask for the peak you expect; under-asking can push the Mac into swap.
- `--gpu none` for CPU-only work (it can run beside a GPU job).
- `--checkpoint atomic` unless the program saves to $GSCHED_CHECKPOINT_DIR, writes READY,
  and exits 0 on SIGUSR1 (then use `cooperative`). Model servers you can restart freely: `restartable`.
- Jobs estimated over 2h go to the night queue (23:00–08:00) automatically.
- If you have MCP tools instead: `submit_job`, then `get_job` with `wait_s: 30`. Do not loop rapidly.
- Never kill processes you did not start. Never use `cancel --terminate` without the user's OK.
````

## 8. Tests (PR 8)

`test_mcp_initialize_negotiates_supported_version`, `test_mcp_initialize_unknown_version_returns_latest`, `test_mcp_tools_list_six_tools`, `test_mcp_and_cli_submit_identical_rows` (compare stored columns except `id`, `enqueued_ms`, `submitted_ms`, `door`, `client_pid`), `test_submit_job_returns_without_waiting` (spec estimate 24 h; returns on the inserting turn), `test_mcp_fills_schema_field`, `test_mcp_rejects_external_spawn`, `test_mcp_forbidden_without_project_opt_in`, `test_mcp_tools_call_is_one_json_line` (no `Content-Length`; parses as JSON; contains `"jsonrpc":"2.0"`), `test_mcp_log_line_not_on_stdout`, `test_mcp_observe_mode_text_prefix`, `test_mcp_daemon_down_is_tool_error`, `test_mcp_no_lease_in_any_output`, `test_daemon_import_does_not_load_mcp`, `test_mcp_schema_matches_validator`, `test_get_job_wait_s_capped_at_30`.
