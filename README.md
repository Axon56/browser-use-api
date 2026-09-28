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
| Sessions | `bu_api/sessions.py` | Creates and stops a dedicated Chrome per session, remembered on disk |
| HTTP | `bu_api/server.py` | FastAPI routes, one daemon lock per session |

Each session gets its own Chrome on a free debug port plus its own browser-harness
daemon, so sessions are fully isolated and can be driven in parallel.

## Running it

```bash
./start.sh          # background, pidfile-managed
./stop.sh
```

It expects a Python 3.11+ virtualenv at `.venv` with `browser-use` and `fastapi`
installed, and Chrome available (set `BU_CHROME_BINARY` to override discovery).
Environment knobs: `BU_API_HOST`, `BU_API_PORT`, `BU_API_KEY`, `BU_API_LOG`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/sessions` | Open a browser session, returns `session_id` |
| `GET` | `/sessions` | List sessions |
| `GET` | `/sessions/{id}` | One session: browser, daemon, current tab |
| `DELETE` | `/sessions/{id}` | Close a session and its browser |
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

## Notes

`/run` executes arbitrary Python in the server process, and `/cli` shells into the
browser-use CLI. Both are powerful; bind the API to localhost and set `BU_API_KEY`
if you expose it beyond your machine.
