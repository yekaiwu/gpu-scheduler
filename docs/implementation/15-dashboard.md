# 15: Dashboard (PR 11)

A read-only web dashboard showing capacity, running jobs, the queue with reasons, recent results, and a memory timeline. It was added at the owner's request after the v2 design. It replaces the "no web UI" non-goal **for a read-only, loopback-only, opt-in view**. The daemon itself still opens no TCP port.

## 1. Architecture

- `gsched dashboard [--port 8765] [--host 127.0.0.1] [--open] [--print-url]` is a **separate process** (not the daemon). It's an HTTP server using stdlib `http.server.ThreadingHTTPServer` (threads are fine here; the one-thread rule applies to the daemon only). It talks to the daemon via `gsched.client.call(..., door="dashboard")`.
- **Read-only.** The dashboard may call only `status`, `list`, `get`, `logs`, `events`, and `ping`. Enforce this with an allowlist in `dashboard.py`, and add a daemon-side check: `door == "dashboard"` with any other method → `forbidden`.
- **Bind loopback only.** `--host` accepts only `127.0.0.1`, `::1`, or `localhost`. Anything else → exit 64.
- **Token.** On first start, create `$H/run/dashboard.token` (0600, `secrets.token_urlsafe(24)`) and reuse it afterwards. Every request needs `?t=<token>` or the cookie `gsched_t=<token>` (set `HttpOnly; SameSite=Strict` on the first good request). Otherwise → 403. `--print-url` prints `http://127.0.0.1:8765/?t=…`.
- **DNS-rebinding guard.** Reject a request whose `Host` header isn't `127.0.0.1:<port>`, `localhost:<port>`, or `[::1]:<port>` (→ 421).
- Response headers: `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'`, `X-Frame-Options: DENY`, `Cache-Control: no-store`, and `Referrer-Policy: no-referrer`.
- **No external assets.** One self-contained HTML page served from `gsched/dashboard_page.html` (package data), with inline CSS and JS, and no CDN or fonts from the network. Charts are hand-drawn SVG.
- If the daemon is unreachable, the API returns 503 `{"error":"daemon_unreachable"}` and the page shows a banner. The dashboard keeps running.

## 2. Daemon addition: `events` RPC

| Method | Params | Result |
|---|---|---|
| `events` | `kinds?` [str], `since_ms?` (default now − 6 h), `job?`, `limit?` (default 2000, max 10000) | `{events:[{t_ms, kind, job, body}]}` oldest first |

It's read-only. Bodies already exclude secrets (12 §1). Also add `list` param `since_ms` so recent terminal jobs can be fetched (`state` in terminal states and `released_ms >= since_ms`).

## 3. HTTP API (dashboard process)

| Path | Proxies to | Notes |
|---|---|---|
| `GET /` | — | The HTML page |
| `GET /api/status` | `status` | As-is |
| `GET /api/jobs` | `list` non-terminal + `list` terminal with `since_ms = now − 24h` | `{active:[…], recent:[…]}` |
| `GET /api/job/<id>` | `get` | Strip `env_keys`. Show `command` as argv0 basename + arg count only (the page is a browser surface) |
| `GET /api/logs/<id>?stream=stdout\|stderr` | `logs` tail 16384 | Text |
| `GET /api/timeline?hours=6` | `events kinds=[sample]` | `[{t_ms, reclaimable_gib, reserved_gib, pending_gib, budget_gib, swap_used_gib, level, gpu_util, running}]` |
| `GET /api/events?hours=24` | `events` (all kinds except `sample`) | Latest 200, newest first |

Validate `<id>` against `^[0-9T]{15}-[0-9a-f]{4}$` before proxying.

## 4. Page layout

Auto-refresh every 2 s (status + jobs) and every 30 s (timeline + events). Pause refreshing when the tab is hidden (`document.visibilityState`). Light and dark via `prefers-color-scheme`, using CSS variables. Must be usable at 1280 px and readable on a phone (single column below 720 px).

1. **Header strip:** machine (chip, memsize), badges for `enforce` (`OBSERVE` in amber when off), `pressure` (normal/warn/critical), `protective`, `draining`, `sleep prevented`, and the night window with a countdown ("night in 6h12m" / "morning edge in 1h03m").
2. **Capacity cards:**
   - Memory: a stacked horizontal bar of the budget: reserved by running jobs (solid), pending inside reserved (hatched), free budget. A second thin bar for live: reclaimable − pending − headroom vs. needs of the top queued job (marker). Labels in GiB.
   - GPU: exclusive holder / shared slots `n/max`, device util %.
   - CPU: `used / budget` cores.
   - Swap: used/total, plus the 60 s rise rate.
3. **Running jobs table:** id (short), project, name, checkpoint mode chip, GPU chip, a footprint-vs-reservation mini bar (red when over), runtime with a progress bar against `estimate_s` (striped when over, labeled `overrun`), `held_reason`, flags (spilled, over_limit, cancel_requested).
4. **Queue table**, ordered by `position`: position, id, project, name, priority, queue (day/night), mem/gpu/cpu ask, waiting time, `blocked_by` chips (color per category: memory, gpu, cpu, lock, time window, policy), and the `why` sentence. The reservation row gets a "reserved" badge. `needs_estimate` rows show the exact `gsched estimate` command to copy.
5. **Memory timeline (6 h):** an SVG line/area chart: budget (dashed line), reserved (area), reclaimable (line), swap used (secondary line). Admit/exit/signal/pressure events are vertical tick marks with tooltips. Night window shaded.
6. **Recent (24 h):** finished jobs with state chip (done/failed/cancelled/lost), runtime, peak footprint vs. request, end_reason.
7. **Events feed:** the last 50 non-sample events, human-formatted.
8. **Job drawer:** clicking any row opens a side panel with the full `get` view (minus env), a stdout/stderr tab (16 KiB tail, a "refresh" button, monospace, auto-scroll), and the copyable CLI commands `gsched get/logs/cancel ID`. There are no action buttons: the dashboard is read-only.

Accessibility: semantic tables, chips with text (not only color), `title` tooltips, and keyboard-closable drawer (Esc).

## 5. Install integration

`gsched install --dashboard [--dashboard-port 8765]` also renders `deploy/com.gpuscheduler.dashboard.plist.template` (label `<launchd_label>.dashboard`, `ProgramArguments` `[venv python, -I, -m, gsched, dashboard, --port, N]`, `KeepAlive` true, `ProcessType` `Background`; the dashboard is allowed to be throttled) and loads it. `gsched dashboard --print-url` prints the tokenized URL. `uninstall` removes both plists.

## 6. Tests

`test_dashboard_binds_loopback_only`, `test_dashboard_rejects_non_loopback_host_arg`, `test_dashboard_requires_token`, `test_dashboard_bad_host_header_421`, `test_dashboard_only_readonly_methods` (patch `client.call` and crawl every route), `test_daemon_forbids_mutation_from_dashboard_door`, `test_dashboard_html_has_no_external_urls` (no `http://` or `https://` in the page except in comments), `test_dashboard_job_view_strips_env_and_args`, `test_dashboard_503_when_daemon_down`, `test_events_rpc_filters_and_limits`, `test_list_since_ms_recent_terminal`, `test_timeline_shape`. Manual check: open it in a browser during the smoke test and confirm all sections render with live data.
