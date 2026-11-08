"""MCP stdio adapter (10). A stateless proxy to the daemon socket with door="mcp". No SDK, no scheduling logic.

Transport: newline-delimited JSON-RPC 2.0. stdout carries protocol messages ONLY; everything else goes to stderr.
Requests are handled sequentially: a get_job with wait_s blocks this adapter for up to 30 s, so a
`notifications/cancelled` for it is only read after the wait returns.
"""

from __future__ import annotations

import json
import os
import sys

from gsched import __version__, client
from gsched.spec import spec_json_schema

SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26"]
INSTRUCTIONS = ("Schedules GPU/CPU/memory-heavy jobs on this Mac. submit_job returns immediately with a job id; poll "
                "get_job (use wait_s up to 30) or ask the user to run `gsched wait ID`. Never assume a job ran: check "
                "state. Set checkpoint to atomic unless the program handles SIGUSR1 per the cooperative contract.")
NOT_RUNNING = "gsched daemon is not running; ask the user to run `gsched install` or `gsched daemon`"
OBSERVE_PREFIX = "NOT QUEUED (observe mode): "


def log(msg: str) -> None:
    print(f"gsched mcp: {msg}", file=sys.stderr, flush=True)


# ---- tool definitions ------------------------------------------------------------------------------------------------
def _obj(props: dict, required: list | None = None) -> dict:
    s = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


TOOLS = [
    {
        "name": "submit_job",
        "description": ("Queue a job on this Mac's GPU/memory scheduler. Returns immediately with a job id. Does NOT "
                        "wait for the job to run. Required: project, command, cwd, checkpoint, resources.memory_gib. "
                        "Use checkpoint=atomic unless the program implements the SIGUSR1 -> READY -> exit 0 contract."),
        "inputSchema": spec_json_schema(schema_required=False),
        "outputSchema": {"type": "object", "properties": {"job": {"type": ["string", "null"]}, "state": {"type": "string"},
                                                          "queue": {"type": ["string", "null"]},
                                                          "warnings": {"type": "array", "items": {"type": "string"}}}},
    },
    {
        "name": "get_job",
        "description": ("Get a job's state, why it is waiting (blocked_by/why), and exit info. Pass wait_s (<=30) to "
                        "wait for a state change instead of polling rapidly."),
        "inputSchema": _obj({"job": {"type": "string"}, "wait_s": {"type": "integer", "minimum": 0, "maximum": 30}},
                            ["job"]),
        "outputSchema": {"type": "object", "properties": {"job": {"type": "string"}, "state": {"type": "string"}}},
    },
    {
        "name": "list_jobs",
        "description": "List jobs, newest first. Filter by state and/or project.",
        "inputSchema": _obj({"state": {"type": ["string", "array"], "items": {"type": "string"}},
                             "project": {"type": "string"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
        "outputSchema": {"type": "object", "properties": {"jobs": {"type": "array"}}},
    },
    {
        "name": "cancel_job",
        "description": ("Cancel a job. Queued jobs are removed. Running atomic jobs are NOT stopped unless "
                        "terminate=true (unsaved work is lost; may be disabled for the project)."),
        "inputSchema": _obj({"job": {"type": "string"}, "terminate": {"type": "boolean", "default": False}}, ["job"]),
        "outputSchema": {"type": "object", "properties": {"job": {"type": "string"}, "state": {"type": "string"},
                                                          "action": {"type": "string"}}},
    },
    {
        "name": "get_job_logs",
        "description": "Tail of a job's stdout or stderr (at most 16 KiB).",
        "inputSchema": _obj({"job": {"type": "string"}, "stream": {"type": "string", "enum": ["stdout", "stderr"],
                                                                   "default": "stdout"},
                             "tail_bytes": {"type": "integer", "minimum": 1, "maximum": 16384, "default": 4096}},
                            ["job"]),
    },
    {
        "name": "get_status",
        "description": ("Capacity summary: memory budget, free, GPU occupancy, pressure, running jobs and queue depth by "
                        "blocked_by reason. Check this before submitting."),
        "inputSchema": _obj({}),
        "outputSchema": {"type": "object", "properties": {"memory": {"type": "object"}, "running": {"type": "array"}}},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}


# ---- a small JSON-Schema validator (the subset used above) --------------------------------------------------------------
def _type_ok(t: str, v) -> bool:
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    if t == "null":
        return v is None
    return True


def validate(schema: dict, value, path: str = "arguments") -> str | None:
    """Returns an error string or None."""
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(x, value) for x in types):
            return f"{path} must be of type {'/'.join(types)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be >= {schema['minimum']}"
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return f"{path} must be > {schema['exclusiveMinimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be <= {schema['maximum']}"
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        return f"{path} is too long"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                return f"{path}.{r} is required"
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    return f"{path}.{k} is not a known field"
        elif isinstance(schema.get("additionalProperties"), dict):
            for k, v in value.items():
                if k not in props:
                    e = validate(schema["additionalProperties"], v, f"{path}.{k}")
                    if e:
                        return e
        for k, sub in props.items():
            if k in value:
                e = validate(sub, value[k], f"{path}.{k}")
                if e:
                    return e
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            e = validate(schema["items"], v, f"{path}[{i}]")
            if e:
                return e
    return None


def _strip_lease(obj):
    """No lease may appear in any MCP output."""
    if isinstance(obj, dict):
        return {k: _strip_lease(v) for k, v in obj.items() if k != "lease"}
    if isinstance(obj, list):
        return [_strip_lease(v) for v in obj]
    return obj


def _dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ---- the server -------------------------------------------------------------------------------------------------------------
class McpServer:
    def __init__(self, call=None):
        self.call = call or client.call
        self.version: str | None = None
        self.initialized = False
        self.got_initialized_notification = False
        self._warned = False

    # JSON-RPC helpers
    @staticmethod
    def _error(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    @staticmethod
    def _result(mid, result) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method = msg.get("method")
        has_id = "id" in msg
        mid = msg.get("id")
        if not isinstance(method, str):
            return self._error(mid, -32600, "invalid request: method must be a string") if has_id else None
        if not has_id:                      # notification: never answered
            if method == "notifications/initialized":
                self.got_initialized_notification = True
            return None
        params = msg.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(mid, -32602, "params must be an object")
        if method == "initialize":
            return self._initialize(mid, params)
        if method == "ping":
            return self._result(mid, {})
        if not self.initialized:
            return self._error(mid, -32002, "server not initialized")
        if not self.got_initialized_notification and not self._warned:
            self._warned = True
            log("request before notifications/initialized (served anyway)")
        if method == "tools/list":
            return self._result(mid, {"tools": self._tools_for_version()})
        if method == "tools/call":
            return self._tools_call(mid, params)
        return self._error(mid, -32601, f"method not found: {method}")

    def _initialize(self, mid, params):
        req = params.get("protocolVersion")
        self.version = req if req in SUPPORTED else SUPPORTED[0]
        self.initialized = True
        return self._result(mid, {
            "protocolVersion": self.version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "gpu-scheduler", "version": __version__},
            "instructions": INSTRUCTIONS})

    def _structured(self) -> bool:
        return self.version is not None and self.version >= "2025-06-18"

    def _tools_for_version(self) -> list:
        out = []
        for t in TOOLS:
            t2 = {k: v for k, v in t.items() if k != "outputSchema" or self._structured()}
            out.append(t2)
        return out

    def _tools_call(self, mid, params):
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if name not in TOOL_NAMES:
            return self._error(mid, -32602, f"unknown tool: {name!r}")
        tool = next(t for t in TOOLS if t["name"] == name)
        err = validate(tool["inputSchema"], args)
        if err:
            return self._error(mid, -32602, f"invalid arguments: {err}")
        try:
            text, structured, is_error = self._run_tool(name, args)
        except client.ConnectError:
            return self._result(mid, {"content": [{"type": "text", "text": NOT_RUNNING}], "isError": True})
        except (client.ProtocolError, ConnectionError, OSError) as e:
            return self._result(mid, {"content": [{"type": "text", "text": f"gsched daemon connection problem: {e}"}],
                                      "isError": True})
        res = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if structured is not None and self._structured():
            res["structuredContent"] = structured
        return self._result(mid, res)

    def _daemon(self, method: str, params: dict, timeout: float | None = 10.0):
        env = self.call(method, params, door="mcp", timeout=timeout)
        if env.get("ok"):
            return _strip_lease(env["result"]), None
        return None, {"error": env.get("error", {"code": "internal", "message": "malformed response"})}

    def _run_tool(self, name: str, a: dict):
        if name == "submit_job":
            spec = dict(a)
            spec.setdefault("schema", "gpu-scheduler.job.v1")
            res, err = self._daemon("submit", {"spec": spec})
        elif name == "get_job":
            p = {"job": a["job"]}
            wait = a.get("wait_s")
            if wait:
                p["wait_s"] = wait
            res, err = self._daemon("get", p, timeout=(wait or 0) + 15)
        elif name == "list_jobs":
            p = {k: a[k] for k in ("state", "project") if k in a}
            p["limit"] = a.get("limit", 100)
            res, err = self._daemon("list", p)
        elif name == "cancel_job":
            res, err = self._daemon("cancel", {"job": a["job"], "terminate": bool(a.get("terminate", False))})
        elif name == "get_job_logs":
            res, err = self._daemon("logs", {"job": a["job"], "stream": a.get("stream", "stdout"),
                                             "tail_bytes": a.get("tail_bytes", 4096)})
            if err is None:
                return res["data"], None, False
        else:
            res, err = self._daemon("status", {"brief": True})
        if err is not None:
            return _dumps(err), err, True
        text = _dumps(res)
        if name == "submit_job" and res.get("state") == "observe":
            text = OBSERVE_PREFIX + text
        return text, res, False


def serve(lines, out, server: McpServer | None = None) -> None:
    server = server or McpServer()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            resp = McpServer._error(None, -32700, "parse error")
        else:
            try:
                resp = server.handle(msg)
            except Exception as e:      # never die on a bug in one request
                log(f"internal error: {e!r}")
                resp = McpServer._error(msg.get("id") if isinstance(msg, dict) else None, -32603, "internal error")
        if resp is not None:
            out.write(_dumps(resp) + "\n")
            out.flush()


def main() -> int:
    out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr           # stray prints can never corrupt the protocol stream
    try:
        serve((line.decode("utf-8", "replace") for line in sys.stdin.buffer), out)
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
