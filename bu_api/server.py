"""FastAPI wrapper that exposes the whole browser-use / browser-harness surface over HTTP.

Three ways to drive it:

* ``POST /tools/{name}``  - one HTTP call per CLI helper (page_info, click_at_xy, ...)
* ``POST /run``           - run a Python snippet with the CLI helpers pre-imported
* ``POST /batch``         - run a sequence of tool calls in one round trip

The browser daemon keeps the tab attached between requests, so a session started
with one call is still there on the next one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import os
import shlex
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from . import __version__
from . import sessions as session_store
from .registry import GROUPS, TOOL_SPECS, call_tool, describe_tools

# Env vars that select which daemon/browser a request talks to. Saved and
# restored per request so concurrent callers do not clobber each other.
SESSION_ENV = (
    "BU_NAME",
    "BU_CDP_URL",
    "BU_CDP_WS",
    "BU_AUTOSPAWN",
    "BH_REQUIRE_EXISTING_DAEMON",
    "BH_TAB_MARKER",
    "BH_RECORD",
    "BH_DOMAIN_SKILLS",
    "BROWSER_USE_API_KEY",
)

API_KEY = os.environ.get("BU_API_KEY")
DEFAULT_SESSION = os.environ.get("BU_NAME", "default")

app = FastAPI(
    title="browser-use API",
    version=__version__,
    description="HTTP access to every browser-use CLI command: direct CDP control, tabs, "
    "waits, screenshots, recordings and cloud browsers. Sessions persist across requests.",
)

_session_locks: dict[str, asyncio.Lock] = {}


# --------------------------------------------------------------------- utils ---
def _check_auth(key: str | None) -> None:
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def _to_jsonable(obj: Any) -> Any:
    """Make helper return values JSON-safe without losing information."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return {"base64": base64.b64encode(obj).decode(), "bytes": len(obj)}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):
        with contextlib.suppress(Exception):
            return _to_jsonable(obj.model_dump())
    if hasattr(obj, "__dict__"):
        with contextlib.suppress(Exception):
            return _to_jsonable({k: v for k, v in vars(obj).items() if not k.startswith("_")})
    return repr(obj)


@contextlib.contextmanager
def _session_env(session: str | None, cdp_url: str | None = None, cdp_ws: str | None = None):
    """Point the helpers at a daemon/browser for the duration of one request."""
    saved = {k: os.environ.get(k) for k in SESSION_ENV}
    try:
        if session:
            os.environ["BU_NAME"] = session
        if cdp_url:
            os.environ["BU_CDP_URL"] = cdp_url
        if cdp_ws:
            os.environ["BU_CDP_WS"] = cdp_ws
        if session:
            # helpers caches the daemon name in a module global at import time.
            from browser_harness import helpers

            helpers.NAME = session
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with contextlib.suppress(Exception):
            from browser_harness import helpers

            helpers.NAME = os.environ.get("BU_NAME", DEFAULT_SESSION)


def _lock_for(session: str) -> asyncio.Lock:
    if session not in _session_locks:
        _session_locks[session] = asyncio.Lock()
    return _session_locks[session]


def _resolve_session(name: str, cdp_url: str | None = None) -> Any:
    """Find the registered session, relaunching its browser if it died.

    Returns the Session, or None for the ambient default session (which attaches
    to whatever Chrome/BU_CDP_URL the process was started with).
    """
    existing = session_store.get_session(name)
    if existing is not None:
        if existing.managed and not session_store.chrome_alive(existing):
            return session_store.create(
                name,
                headless=existing.headless,
                start_url=existing.start_url,
                profile_dir=existing.profile_dir,
            )
        session_store.touch(name)
        return existing
    if name == DEFAULT_SESSION and not cdp_url:
        return None
    return session_store.create(name, cdp_url=cdp_url)


def _ensure_daemon(session: str, cdp_url: str | None) -> None:
    """Make sure a daemon exists and is attached to this session's browser."""
    from browser_harness import admin

    env = {"BU_CDP_URL": cdp_url} if cdp_url else None
    admin.ensure_daemon(None, session, env)


def _run_sync(fn, *args, **kwargs):
    """Run a blocking helper in a worker thread with stdout/stderr captured."""
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        result = fn(*args, **kwargs)
    return result, buf_out.getvalue(), buf_err.getvalue()


# --------------------------------------------------------------------- routes ---
@app.get("/health")
async def health() -> dict:
    """Liveness plus a cheap view of daemon/browser state."""
    info: dict[str, Any] = {"status": "ok", "version": __version__}
    try:
        from browser_harness import admin

        info["daemon_alive"] = bool(admin.daemon_alive(DEFAULT_SESSION))
        info["browser"] = admin.daemon_browser_kind(DEFAULT_SESSION)
    except Exception as exc:
        info["status"] = "degraded"
        info["error"] = str(exc)
    return info


@app.get("/tools")
async def tools(x_api_key: str | None = Header(default=None)) -> dict:
    """Self-describing catalogue of every command, with JSON schemas."""
    _check_auth(x_api_key)
    return {"count": len(TOOL_SPECS), "groups": GROUPS, "tools": describe_tools()}


@app.post("/tools/{name}")
async def call_tool_route(
    name: str,
    payload: dict = Body(default_factory=dict),
    session_id: str | None = Query(default=None, description="Session to run in (from POST /sessions)"),
    session: str | None = Query(default=None, description="Alias for session_id"),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Run a single CLI command."""
    _check_auth(x_api_key)
    if name not in TOOL_SPECS:
        raise HTTPException(status_code=404, detail=f"unknown tool '{name}'; see GET /tools")

    payload = payload or {}
    session = session_id or session or payload.get("session_id") or payload.get("session") or DEFAULT_SESSION
    args = {k: v for k, v in payload.items() if k not in {"session", "session_id", "cdp_url", "cdp_ws"}}
    started = time.perf_counter()
    out, err = "", ""

    try:
        resolved = await asyncio.to_thread(_resolve_session, session, payload.get("cdp_url"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    cdp_url = resolved.cdp_url if resolved else payload.get("cdp_url")

    async with _lock_for(session):
        with _session_env(session, cdp_url, payload.get("cdp_ws")):
            try:
                await asyncio.to_thread(_ensure_daemon, session, cdp_url)
                result, out, err = await asyncio.to_thread(_run_sync, call_tool, name, args)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"unknown tool '{name}'") from None
            except TypeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
            except Exception as exc:
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "tool": name,
                        "error": f"{type(exc).__name__}: {exc}",
                        "stdout": out,
                        "stderr": err,
                    },
                )

    body: dict[str, Any] = {
        "ok": True,
        "tool": name,
        "session": session,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "result": _to_jsonable(result),
    }
    if out.strip():
        body["stdout"] = out
    if err.strip():
        body["stderr"] = err
    return body


@app.post("/run")
async def run_code(
    payload: dict = Body(...),
    session_id: str | None = Query(default=None),
    session: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Execute a Python snippet exactly like ``browser-use <<'PY' ... PY``.

    Every CLI helper (new_tab, page_info, js, cdp, click_at_xy, ...) is already in scope.
    """
    _check_auth(x_api_key)
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=422, detail="'code' must be a non-empty string")

    session = session_id or session or payload.get("session_id") or payload.get("session") or DEFAULT_SESSION
    started = time.perf_counter()
    out, err = io.StringIO(), io.StringIO()

    try:
        resolved = await asyncio.to_thread(_resolve_session, session, payload.get("cdp_url"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    cdp_url = resolved.cdp_url if resolved else payload.get("cdp_url")

    async with _lock_for(session):
        with _session_env(session, cdp_url, payload.get("cdp_ws")):
            try:
                from browser_harness import admin, helpers

                if not code.lstrip().startswith(("start_remote_daemon(", "stop_remote_daemon(")):
                    await asyncio.to_thread(_ensure_daemon, session, cdp_url)

                scope: dict[str, Any] = dict(vars(helpers))
                scope["admin"] = admin
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exec(compile(code, "<api-run>", "exec"), scope)
            except Exception as exc:
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                        "stdout": out.getvalue(),
                        "stderr": err.getvalue(),
                    },
                )

    return {
        "ok": True,
        "session": session,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
    }


@app.post("/screenshot")
async def screenshot(
    payload: dict = Body(default_factory=dict),
    session_id: str | None = Query(default=None),
    session: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
) -> Response:
    """Capture the current viewport. Returns a PNG by default, base64 JSON with ``?format=json``."""
    _check_auth(x_api_key)
    payload = payload or {}
    session = session_id or session or payload.get("session_id") or payload.get("session") or DEFAULT_SESSION
    fmt = payload.get("format", "png")

    try:
        resolved = await asyncio.to_thread(_resolve_session, session, payload.get("cdp_url"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    cdp_url = resolved.cdp_url if resolved else payload.get("cdp_url")

    async with _lock_for(session):
        with _session_env(session, cdp_url):
            try:
                from browser_harness import helpers

                await asyncio.to_thread(_ensure_daemon, session, cdp_url)
                result, _, _ = await asyncio.to_thread(
                    _run_sync,
                    helpers.capture_screenshot,
                    None,
                    bool(payload.get("full")),
                    payload.get("max_dim"),
                )
                data = Path(result).read_bytes()
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"screenshot failed: {exc}") from None

    if fmt == "json":
        return JSONResponse(
            {
                "ok": True,
                "path": str(result),
                "base64": base64.b64encode(data).decode(),
                "bytes": len(data),
            }
        )
    return Response(content=data, media_type="image/png")


@app.get("/file")
async def get_file(
    path: str = Query(..., description="Absolute path to a screenshot or recording artifact"),
    x_api_key: str | None = Header(default=None),
) -> Response:
    """Fetch an artifact produced by the browser (screenshots, recordings, videos)."""
    _check_auth(x_api_key)
    target = Path(path).expanduser().resolve()
    allowed_roots = [Path("/tmp"), Path.home() / ".config" / "browser-harness", Path(os.environ.get("BH_HOME", "")) if os.environ.get("BH_HOME") else None]
    if not any(root and str(target).startswith(str(root.resolve())) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="path outside allowed artifact roots")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return Response(content=target.read_bytes(), media_type="application/octet-stream")


# --------------------------------------------------------------------- batch ---
@app.post("/batch")
async def batch(
    payload: dict = Body(...),
    session_id: str | None = Query(default=None),
    session: str | None = Query(default=None),
    stop_on_error: bool = Query(default=True, description="Abort the sequence on the first failure"),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Run several commands in order within one request.

    Body: ``{"steps": [{"tool": "new_tab", "args": {...}}, ...]}``. Each step
    returns the same shape as ``POST /tools/{name}``.
    """
    _check_auth(x_api_key)
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(status_code=422, detail="'steps' must be a non-empty list")

    session = session_id or session or payload.get("session_id") or payload.get("session") or DEFAULT_SESSION
    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    try:
        resolved = await asyncio.to_thread(_resolve_session, session, payload.get("cdp_url"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    cdp_url = resolved.cdp_url if resolved else payload.get("cdp_url")

    async with _lock_for(session):
        with _session_env(session, cdp_url, payload.get("cdp_ws")):
            await asyncio.to_thread(_ensure_daemon, session, cdp_url)
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or "tool" not in step:
                    raise HTTPException(status_code=422, detail=f"step {index} needs a 'tool' field")
                name = step["tool"]
                if name not in TOOL_SPECS:
                    raise HTTPException(status_code=404, detail=f"step {index}: unknown tool '{name}'")
                step_started = time.perf_counter()
                try:
                    result, out, err = await asyncio.to_thread(_run_sync, call_tool, name, step.get("args") or {})
                except Exception as exc:
                    results.append({"step": index, "tool": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                    if stop_on_error:
                        break
                    continue
                entry: dict[str, Any] = {
                    "step": index,
                    "tool": name,
                    "ok": True,
                    "duration_ms": round((time.perf_counter() - step_started) * 1000, 1),
                    "result": _to_jsonable(result),
                }
                if out.strip():
                    entry["stdout"] = out
                if err.strip():
                    entry["stderr"] = err
                results.append(entry)

    return {
        "ok": all(r.get("ok") for r in results),
        "session": session,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "results": results,
    }


# ------------------------------------------------------------------ sessions ---
@app.post("/cli")
async def run_cli(
    payload: dict = Body(...),
    session_id: str | None = Query(default=None),
    session: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Run any ``browser-use`` subcommand and return its exit code and output.

    Body: ``{"command": ["doctor", "--json"]}`` or ``{"command": "doctor --json"}``.
    This is the escape hatch that covers every command the CLI has, including the
    ones without a typed endpoint (install, init, skill, auth, video, telemetry,
    update, reload, recordings).
    """
    _check_auth(x_api_key)
    command = payload.get("command")
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, list) and all(isinstance(a, str) for a in command):
        argv = list(command)
    else:
        raise HTTPException(status_code=422, detail="'command' must be a string or list of strings")

    payload = payload or {}
    session = session_id or session or payload.get("session_id") or payload.get("session") or DEFAULT_SESSION
    try:
        resolved = await asyncio.to_thread(_resolve_session, session, payload.get("cdp_url"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    cdp_url = resolved.cdp_url if resolved else payload.get("cdp_url")

    out, err = io.StringIO(), io.StringIO()

    def _invoke() -> int:
        from browser_use.cli import main as cli_main

        saved_argv = sys.argv
        sys.argv = ["browser-use", *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    result = cli_main()
                except SystemExit as exc:
                    return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            return result if isinstance(result, int) else 0
        finally:
            sys.argv = saved_argv

    async with _lock_for(session):
        with _session_env(session, cdp_url, payload.get("cdp_ws")):
            exit_code = await asyncio.to_thread(_invoke)

    return {
        "ok": exit_code == 0,
        "command": argv,
        "session": session,
        "exit_code": exit_code,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
    }


# ------------------------------------------------------------------ sessions ---
@app.post("/sessions", status_code=201)
async def create_session(
    payload: dict = Body(default_factory=dict),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Open a browser session. Returns the ``session_id`` to pass to every later call.

    Body (all optional):

      ``id``          session name; generated when omitted
      ``cdp_url``     attach to an existing Chrome instead of launching one
      ``headless``    launch without a visible window (default true)
      ``start_url``   page to open on launch
      ``profile_dir`` user-data-dir to reuse (persistent cookies/logins)
    """
    _check_auth(x_api_key)
    payload = payload or {}
    name = payload.get("id") or payload.get("name") or f"s{uuid.uuid4().hex[:10]}"
    try:
        session = await asyncio.to_thread(
            session_store.create,
            name,
            payload.get("cdp_url"),
            bool(payload.get("headless", True)),
            payload.get("start_url"),
            payload.get("profile_dir"),
        )
        await asyncio.to_thread(_ensure_daemon, name, session.cdp_url)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not create session: {exc}") from None

    body = session.public()
    body["ok"] = True
    return body


@app.get("/sessions")
async def list_sessions(x_api_key: str | None = Header(default=None)) -> dict:
    """List every session the API is tracking."""
    _check_auth(x_api_key)
    return {"default": DEFAULT_SESSION, "sessions": session_store.list_sessions()}


@app.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Status of one session: browser alive, daemon alive, current tab."""
    _check_auth(x_api_key)
    session = session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"unknown session '{session_id}'")
    body = session.public()
    try:
        from browser_harness import admin

        body["daemon_alive"] = bool(admin.daemon_alive(session_id))
        body["browser_kind"] = admin.daemon_browser_kind(session_id)
        if body["daemon_alive"]:
            with _session_env(session_id, session.cdp_url):
                from browser_harness import helpers

                body["current_tab"] = _to_jsonable(helpers.current_tab())
    except Exception as exc:
        body["probe_error"] = str(exc)
    return body


@app.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    stop_browser: bool = Query(default=True, description="Also stop a browser this API launched"),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Close a session: stop its daemon and its browser."""
    _check_auth(x_api_key)
    try:
        result = await asyncio.to_thread(session_store.delete, session_id, stop_browser)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown session '{session_id}'") from None
    result["ok"] = True
    return result

def main() -> None:
    """Console entry point: ``bu-api`` / ``python -m bu_api.server``."""
    import uvicorn

    host = os.environ.get("BU_API_HOST", "127.0.0.1")
    port = int(os.environ.get("BU_API_PORT", "8000"))
    uvicorn.run("bu_api.server:app", host=host, port=port, log_level=os.environ.get("BU_API_LOG_LEVEL", "info"))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
