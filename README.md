# browser-use API

An HTTP wrapper around the [browser-use](https://github.com/browser-use/browser-use) CLI,
so every command it ships becomes a plain HTTP call.

The CLI runs Python piped on stdin (`browser-use <<'PY' ... PY`) and keeps a background
daemon attached to a browser. This project exposes that same surface over HTTP and gives
each browser a persistent, addressable session.

## How it works

Three layers, no LLM involved:

| Layer | File | Role |
| --- | --- | --- |
| Commands | `bu_api/registry.py` | Maps every CLI helper to a JSON schema and a Python function |
| Sessions | `bu_api/sessions.py` | Creates and stops a dedicated browser per session, remembered on disk |
| HTTP | `bu_api/server.py` | FastAPI routes, one daemon lock per session |

Each session gets its own browser on a free debug port plus its own browser-harness
daemon, so sessions are fully isolated and can be driven in parallel.

## Setup

```bash
uv venv --python 3.11 .venv          # any Python 3.11+ venv works
uv pip install --python .venv/bin/python browser-use fastapi uvicorn
./start.sh          # background, pidfile-managed
./stop.sh
```

The server needs a Chromium-family browser (see [Choosing a browser](#choosing-a-browser)).
Environment knobs: `BU_API_HOST`, `BU_API_PORT`, `BU_API_KEY`, `BU_API_LOG`,
`BU_CHROME_BINARY` (explicit browser path), `BU_BROWSER` (default browser family).

## Choosing a browser

The underlying browser-harness stack speaks the Chrome DevTools Protocol, so any
Chromium-family browser works. Pass a family when creating a session:

```bash
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"id":"work","browser":"edge"}'
```

| `browser` value | Resolved from |
| --- | --- |
| `chrome` (default) | `google-chrome-stable`, `google-chrome`, standard Linux/macOS/Windows install paths |
| `chromium` | `chromium`, `chromium-browser`, standard install paths |
| `edge` | `microsoft-edge(-stable)`, standard install paths |
| `brave` | `brave-browser`, `brave`, standard install paths |
| anything else | looked up on `PATH` directly (e.g. `"google-chrome-beta"`) |

Discovery checks `BU_CHROME_BINARY` first (an explicit path always wins), then PATH,
then the usual per-platform install locations. Set `BU_BROWSER` to change the default
family for every session, or pass `"binary":"/path/to/browser"` on a session to skip
discovery entirely.

`GET /browsers` reports what the host can launch:

```bash
curl localhost:8000/browsers
# {"default":"chrome","browsers":{"chrome":{"supported":true,"path":"/usr/bin/google-chrome",...}, ...}}
```

**Firefox and Safari are not supported.** The harness drives browsers over the Chrome
DevTools Protocol only; Firefox dropped its CDP endpoint and Safari never had one.
Asking for either fails with a clear error instead of launching the wrong thing.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/sessions` | Open a browser session, returns `session_id` |
| `GET` | `/sessions` | List sessions |
| `GET` | `/sessions/{id}` | One session: browser, daemon, current tab |
| `DELETE` | `/sessions/{id}` | Close a session and its browser |
| `GET` | `/browsers` | Detected browsers per family, plus unsupported ones with reasons |
| `GET` | `/tools` | List all commands with input schemas |
| `POST` | `/tools/{name}` | Run one command |
| `POST` | `/run` | Run a Python snippet with CLI helpers in scope |
| `POST` | `/batch` | Run several commands in one request |
| `POST` | `/cli` | Run any `browser-use` subcommand |
| `POST` | `/screenshot` | Capture the viewport (PNG, or base64 JSON) |
| `GET` | `/file` | Fetch a screenshot or recording artifact |
| `GET` | `/health` | Liveness and daemon state |

Pass `?session_id=<id>` to any of them to pick the browser. When omitted, the default
session is used.

## Tabs within a browser session

Multi-browser sessions and tabs are different: each `session_id` selects a browser;
within that session, `list_tabs`, `current_tab`, `switch_tab`, `new_tab`, and
`close_tab` were already exposed through `/tools/{name}`. A tab is identified by
its CDP `targetId`, not its URL or its position in the tab list. `new_tab` may
reuse an attached blank tab rather than create a second tab.

```bash
# Create or select a browser session, then list its tab IDs.
curl -X POST 'localhost:8000/tools/list_tabs?session_id=demo' -H 'content-type: application/json' -d '{}'
# Open another tab. Save the returned targetId for later calls.
curl -X POST 'localhost:8000/tools/new_tab?session_id=demo' -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'
# Read or interact with one specific tab without a separate switch request.
curl -X POST 'localhost:8000/tools/page_info?session_id=demo&tab_id=<targetId>' \
  -H 'content-type: application/json' -d '{}'
# Close that tab, using the underlying helper's target argument.
curl -X POST 'localhost:8000/tools/close_tab?session_id=demo' \
  -H 'content-type: application/json' -d '{"target":"<targetId>"}'
```

`tab_id` is supported as a query parameter or top-level JSON field on `/tools`,
`/run`, `/batch`, and `/screenshot`. It attaches the session to that tab before
running the action. It must be an exact `targetId` from `list_tabs` **in that
session**; stale, cross-browser, URL and index values are rejected rather than
silently acting on the wrong page. Omitting `tab_id` uses the currently attached
tab. Selection does not bring a tab to the foreground; use `activate_tab` when
that is intentional. Targeting a tab changes the attached tab for later requests
in the same session.

In `/batch`, a top-level `tab_id` is the default for each step, and any step can
override it with its own `tab_id` alongside `tool` and `args`:

```json
{"steps":[
  {"tool":"page_info","tab_id":"<firstTargetId>","args":{}},
  {"tool":"page_info","tab_id":"<secondTargetId>","args":{}}
]}
```

The session lock keeps these selections and actions ordered within a browser
session. Tabs are not separate parallel execution lanes inside one session.

## Clicking

Prefer `click_selector` over scroll + `click_at_xy`: it scrolls the element into view
**instantly** (a page's own smooth scrolling can slide the target away mid-click), checks
the element is the real hit target at those coordinates, then sends a genuine CDP mouse
click at its center. The click is sent **only** on a true hit: if a cookie bar or promo
overlay covers the target, no click is dispatched and the result tells you what is in the
way, so you can dismiss it and retry instead of clicking the overlay:

```json
{"clicked": null, "selector": "button[type=submit]", "hit_target": false,
 "blocked_by": "div#cookie-banner", "note": "no click dispatched: ..."}
```

`timeout` (default 10s) only bounds waiting for the element to *exist*; once it exists the
hit check runs right away and keeps re-checking for up to `click_timeout` (default 3s).
An element that is present but mid-transition no longer burns the whole timeout.

```bash
curl -X POST 'localhost:8000/tools/click_selector?session_id=work' \
  -H 'content-type: application/json' \
  -d '{"selector":"button[type=submit]","timeout":10}'
```

`page_info` waits for the document to parse before reading it, so calling it right after
`new_tab` no longer crashes with "Cannot read properties of null". Inside `/run` snippets
the plain `page_info()` name is the guarded variant too; the unguarded original stays
reachable as `raw_page_info()`.

## Recording

The browser-use CLI (browser-harness 0.1.13) saves an **action trace**:
`events.jsonl` and a JPEG viewport frame after each supported action, not a
continuous video. The API already exposed `start_recording` and `stop_recording`,
but HTTP calls bypassed the CLI recording hook. `/tools`, `/batch` and `/run`
now notify that same recorder. Recording is off by default and scoped to the
selected session; start it before the actions you want to capture.

```bash
curl -X POST 'localhost:8000/tools/start_recording?session_id=demo' \
  -H 'content-type: application/json' -d '{"name":"my-run"}'
curl -X POST 'localhost:8000/tools/new_tab?session_id=demo' \
  -H 'content-type: application/json' -d '{"url":"https://example.com"}'
curl -X POST 'localhost:8000/tools/stop_recording?session_id=demo' \
  -H 'content-type: application/json' -d '{}'
```

The returned `result` is the recording directory. Download a specific frame
or `events.jsonl` via `GET /file?path=<absolute file path>` with your API key.
Raw `js` and `cdp` calls are not captured: their arbitrary effects and sensitive
content cannot safely be classified as visual actions. The CLI's `video init`,
`video review`, `video export --reviewed` commands remain accessible via
`POST /cli` for a reviewed MP4; export is not automatic. Recordings can contain
private page content and form values. `BH_RECORD=0` disables recording.
`set_auto_recording` changes the default globally for this server, not per session.

## Example

Open a browser, then drive it across separate calls:

```bash
curl -X POST localhost:8000/sessions -H 'content-type: application/json' \
  -d '{"id":"demo"}'

curl -X POST 'localhost:8000/tools/new_tab?session_id=demo' \
  -H 'content-type: application/json' -d '{"url":"https://example.com"}'

curl -X POST 'localhost:8000/tools/page_info?session_id=demo' \
  -H 'content-type: application/json' -d '{}'
```

The tab opened by the first call is still attached on the second — that is the whole
point of the session layer.

`/run` executes snippets in a worker thread off the server event loop, so a slow or
failed snippet never stalls unrelated requests:

```bash
curl -X POST 'localhost:8000/run?session_id=demo' \
  -H 'content-type: application/json' \
  -d '{"code":"new_tab(\"https://example.com\")\nprint(page_info()[\"url\"])"}'
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## Notes

`/run` executes arbitrary Python in the server process, and `/cli` shells into the
browser-use CLI. Both are powerful; bind the API to localhost and set `BU_API_KEY`
if you expose it beyond your machine.
