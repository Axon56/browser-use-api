"""Tool registry: every browser-use / browser-harness capability exposed over HTTP.

Each entry maps a JSON-friendly HTTP tool call onto one Python helper so that the
whole CLI feature surface becomes addressable as an API.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable


def _prop(type_: str, desc: str, default: Any = None, required: bool = False, **extra: Any) -> dict:
    d: dict[str, Any] = {"type": type_, "description": desc}
    if required:
        d["required"] = True
    else:
        d["default"] = default
    if extra:
        d.update(extra)
    return d


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


# name -> (module, attribute, description, schema, group)
TOOL_SPECS: dict[str, dict[str, Any]] = {
    # ---------------------------------------------------------------- core ---
    "page_info": {
        "module": "local",
        "attr": "guarded_page_info",
        "group": "core",
        "description": "Current tab URL, title, viewport and scroll position. Waits for the document to parse instead of crashing right after a navigation.",
        "schema": _schema({}),
    },
    "new_tab": {
        "module": "helpers",
        "attr": "new_tab",
        "group": "core",
        "description": "Open a tab, attach the session to it and navigate. Use this for the first navigation of a task instead of goto_url.",
        "schema": _schema({"url": _prop("string", "URL to open.", "about:blank")}),
    },
    "goto_url": {
        "module": "helpers",
        "attr": "goto_url",
        "group": "core",
        "description": "Navigate the already-attached tab to a URL.",
        "schema": _schema({"url": _prop("string", "Destination URL.", required=True)}, ["url"]),
    },
    "js": {
        "module": "helpers",
        "attr": "js",
        "group": "core",
        "description": "Evaluate a JavaScript expression in the page and return its value.",
        "schema": _schema(
            {
                "expression": _prop("string", "JavaScript expression, e.g. \"document.title\".", required=True),
                "target_id": _prop("string", "Optional CDP target id to evaluate in."),
            },
            ["expression"],
        ),
    },
    "cdp": {
        "module": "helpers",
        "attr": "cdp",
        "group": "core",
        "description": "Send a raw Chrome DevTools Protocol command, e.g. Accessibility.getFullAXTree.",
        "schema": _schema(
            {
                "method": _prop("string", "CDP domain method, e.g. 'DOM.getBoxModel'.", required=True),
                "session_id": _prop("string", "Optional CDP session id."),
                "params": _prop("object", "CDP command parameters (passed as keyword arguments).", {}),
            },
            ["method"],
        ),
    },
    "drain_events": {
        "module": "helpers",
        "attr": "drain_events",
        "group": "core",
        "description": "Return and clear buffered CDP events (network, console, lifecycle).",
        "schema": _schema({}),
    },
    # ------------------------------------------------------------ interact ---
    "click_at_xy": {
        "module": "helpers",
        "attr": "click_at_xy",
        "group": "interact",
        "description": "Click at viewport coordinates. Get them from the accessibility tree box model.",
        "schema": _schema(
            {
                "x": _prop("number", "Viewport x in CSS pixels.", required=True),
                "y": _prop("number", "Viewport y in CSS pixels.", required=True),
                "button": _prop("string", "Mouse button.", "left", enum=["left", "right", "middle"]),
                "clicks": _prop("integer", "Click count.", 1),
            },
            ["x", "y"],
        ),
    },
    "click_selector": {
        "module": "local",
        "attr": "click_selector",
        "group": "interact",
        "description": "Click a CSS-selected element: instant scroll into view, hit-target check, then a real CDP mouse click at its center - dispatched only when the target truly is the hit target. If something covers it (cookie bar, promo overlay), no click is sent and the result reports hit_target=false with a blocked_by description. Prefer this over scroll + click_at_xy so page smooth-scrolling cannot steal the click.",
        "schema": _schema(
            {
                "selector": _prop("string", "CSS selector.", required=True),
                "timeout": _prop("number", "Seconds to wait for the element.", 10.0),
                "button": _prop("string", "Mouse button.", "left", enum=["left", "right", "middle"]),
                "click_timeout": _prop("number", "Seconds to keep re-checking the hit target before giving up (no click is sent on failure).", 3.0),
            },
            ["selector"],
        ),
    },
    "type_text": {
        "module": "helpers",
        "attr": "type_text",
        "group": "interact",
        "description": "Type text into the focused element, character by character.",
        "schema": _schema({"text": _prop("string", "Text to type.", required=True)}, ["text"]),
    },
    "fill_input": {
        "module": "helpers",
        "attr": "fill_input",
        "group": "interact",
        "description": "Fill a CSS-selected input/textarea directly and fire input events.",
        "schema": _schema(
            {
                "selector": _prop("string", "CSS selector.", required=True),
                "text": _prop("string", "Value to set.", required=True),
                "clear_first": _prop("boolean", "Clear the field before typing.", True),
                "timeout": _prop("number", "Seconds to wait for the element.", 0.0),
            },
            ["selector", "text"],
        ),
    },
    "press_key": {
        "module": "helpers",
        "attr": "press_key",
        "group": "interact",
        "description": "Press a single key (e.g. 'Enter', 'Tab') with optional modifiers.",
        "schema": _schema(
            {
                "key": _prop("string", "Key name.", required=True),
                "modifiers": _prop("integer", "CDP modifier bitmask (1=Alt, 2=Ctrl, 4=Meta, 8=Shift).", 0),
            },
            ["key"],
        ),
    },
    "dispatch_key": {
        "module": "helpers",
        "attr": "dispatch_key",
        "group": "interact",
        "description": "Dispatch a key event to a CSS-selected element.",
        "schema": _schema(
            {
                "selector": _prop("string", "CSS selector.", required=True),
                "key": _prop("string", "Key name.", "Enter"),
                "event": _prop("string", "Event type.", "keypress", enum=["keypress", "keydown", "keyup"]),
            },
            ["selector"],
        ),
    },
    "scroll": {
        "module": "helpers",
        "attr": "scroll",
        "group": "interact",
        "description": "Scroll the page from a starting point by a delta.",
        "schema": _schema(
            {
                "x": _prop("number", "Start x.", required=True),
                "y": _prop("number", "Start y.", required=True),
                "dy": _prop("number", "Vertical delta (negative scrolls down).", -300),
                "dx": _prop("number", "Horizontal delta.", 0),
            },
            ["x", "y"],
        ),
    },
    "upload_file": {
        "module": "helpers",
        "attr": "upload_file",
        "group": "interact",
        "description": "Attach a local file to a file input matched by CSS selector.",
        "schema": _schema(
            {
                "selector": _prop("string", "CSS selector for the file input.", required=True),
                "path": _prop("string", "Absolute local file path.", required=True),
            },
            ["selector", "path"],
        ),
    },
    # ---------------------------------------------------------------- tabs ---
    "list_tabs": {
        "module": "helpers",
        "attr": "list_tabs",
        "group": "tabs",
        "description": "List open tabs with their target ids.",
        "schema": _schema({"include_chrome": _prop("boolean", "Include chrome:// and internal tabs.", True)}),
    },
    "current_tab": {
        "module": "helpers",
        "attr": "current_tab",
        "group": "tabs",
        "description": "Return the tab the session is currently attached to.",
        "schema": _schema({}),
    },
    "switch_tab": {
        "module": "helpers",
        "attr": "switch_tab",
        "group": "tabs",
        "description": "Attach the session to another tab by id, url or index.",
        "schema": _schema(
            {
                "target": _prop("string", "Target id, URL substring, or tab index.", required=True),
                "activate": _prop("boolean", "Also bring the tab to the foreground.", False),
            },
            ["target"],
        ),
    },
    "activate_tab": {
        "module": "helpers",
        "attr": "activate_tab",
        "group": "tabs",
        "description": "Bring a tab to the foreground (visibly changes focus).",
        "schema": _schema({"target": _prop("string", "Target id, URL substring, or index.", required=True)}, ["target"]),
    },
    "close_tab": {
        "module": "helpers",
        "attr": "close_tab",
        "group": "tabs",
        "description": "Close a tab, or the current one when no target is given.",
        "schema": _schema({"target": _prop("string", "Target id, URL substring, or index.")}),
    },
    "ensure_real_tab": {
        "module": "helpers",
        "attr": "ensure_real_tab",
        "group": "tabs",
        "description": "Replace an internal/stale tab with a usable page tab.",
        "schema": _schema({}),
    },
    "iframe_target": {
        "module": "helpers",
        "attr": "iframe_target",
        "group": "tabs",
        "description": "Return the CDP target for the first iframe whose URL contains a substring.",
        "schema": _schema({"url_substr": _prop("string", "URL substring to match.", required=True)}, ["url_substr"]),
    },
    # --------------------------------------------------------------- waits ---
    "wait": {
        "module": "helpers",
        "attr": "wait",
        "group": "wait",
        "description": "Sleep for N seconds.",
        "schema": _schema({"seconds": _prop("number", "Seconds to sleep.", 1.0)}),
    },
    "wait_for_load": {
        "module": "helpers",
        "attr": "wait_for_load",
        "group": "wait",
        "description": "Block until document.readyState is 'complete' or timeout.",
        "schema": _schema({"timeout": _prop("number", "Timeout in seconds.", 15.0)}),
    },
    "wait_for_element": {
        "module": "helpers",
        "attr": "wait_for_element",
        "group": "wait",
        "description": "Block until a CSS selector exists (and optionally is visible) or timeout.",
        "schema": _schema(
            {
                "selector": _prop("string", "CSS selector.", required=True),
                "timeout": _prop("number", "Timeout in seconds.", 10.0),
                "visible": _prop("boolean", "Also require the element to be rendered.", False),
            },
            ["selector"],
        ),
    },
    "wait_for_network_idle": {
        "module": "helpers",
        "attr": "wait_for_network_idle",
        "group": "wait",
        "description": "Block until in-flight requests settle and the network is quiet.",
        "schema": _schema(
            {
                "timeout": _prop("number", "Timeout in seconds.", 10.0),
                "idle_ms": _prop("integer", "Required quiet window in milliseconds.", 500),
            }
        ),
    },
    # ---------------------------------------------------------- extract/io ---
    "capture_screenshot": {
        "module": "helpers",
        "attr": "capture_screenshot",
        "group": "io",
        "description": "Save a PNG screenshot of the viewport and return its path.",
        "schema": _schema(
            {
                "path": _prop("string", "Destination path (defaults to a temp file)."),
                "full": _prop("boolean", "Capture the full page beyond the viewport.", False),
                "max_dim": _prop("integer", "Downscale so the longest side is at most this many pixels."),
            }
        ),
    },
    "http_get": {
        "module": "helpers",
        "attr": "http_get",
        "group": "io",
        "description": "Plain HTTP GET without a browser (use when no interaction is needed).",
        "schema": _schema(
            {
                "url": _prop("string", "URL to fetch.", required=True),
                "headers": _prop("object", "Request headers."),
                "timeout": _prop("number", "Timeout in seconds.", 20.0),
            },
            ["url"],
        ),
    },
    # -------------------------------------------------------------- daemon ---
    "daemon_alive": {
        "module": "admin",
        "attr": "daemon_alive",
        "group": "daemon",
        "description": "Whether the browser daemon is running.",
        "schema": _schema({"name": _prop("string", "Daemon name.")}),
    },
    "daemon_browser_kind": {
        "module": "admin",
        "attr": "daemon_browser_kind",
        "group": "daemon",
        "description": "Report whether the daemon drives a local, CDP or cloud browser.",
        "schema": _schema({"name": _prop("string", "Daemon name.")}),
    },
    "ensure_daemon": {
        "module": "admin",
        "attr": "ensure_daemon",
        "group": "daemon",
        "description": "Start the daemon and connect it to Chrome if it is not already up.",
        "schema": _schema(
            {
                "wait": _prop("boolean", "Wait for the daemon to become ready."),
                "name": _prop("string", "Daemon name."),
            }
        ),
    },
    "restart_daemon": {
        "module": "admin",
        "attr": "restart_daemon",
        "group": "daemon",
        "description": "Stop the daemon so the next call starts a fresh one (picks up code changes).",
        "schema": _schema(
            {
                "name": _prop("string", "Daemon name."),
                "require_clean": _prop("boolean", "Fail unless the previous daemon stopped cleanly.", False),
            }
        ),
    },
    "start_remote_daemon": {
        "module": "admin",
        "attr": "start_remote_daemon",
        "group": "cloud",
        "description": "Start a Browser Use Cloud browser and attach a named daemon to it. Bills until stopped.",
        "schema": _schema(
            {
                "name": _prop("string", "Daemon name to register.", "remote"),
                "profileName": _prop("string", "Cloud profile name to load."),
            }
        ),
    },
    "stop_remote_daemon": {
        "module": "admin",
        "attr": "stop_remote_daemon",
        "group": "cloud",
        "description": "Stop a cloud browser daemon and stop billing.",
        "schema": _schema({"name": _prop("string", "Daemon name.", "remote")}),
    },
    "list_cloud_profiles": {
        "module": "admin",
        "attr": "list_cloud_profiles",
        "group": "cloud",
        "description": "List Browser Use Cloud profiles.",
        "schema": _schema({}),
    },
    "list_local_profiles": {
        "module": "admin",
        "attr": "list_local_profiles",
        "group": "cloud",
        "description": "List local Chrome profiles available for cookie sync.",
        "schema": _schema({}),
    },
    "sync_local_profile": {
        "module": "admin",
        "attr": "sync_local_profile",
        "group": "cloud",
        "description": "Push a local Chrome profile's cookies into a cloud profile.",
        "schema": _schema(
            {
                "profile_name": _prop("string", "Local profile name.", required=True),
                "browser": _prop("string", "Browser family to read from."),
                "cloud_profile_id": _prop("string", "Target cloud profile id."),
            },
            ["profile_name"],
        ),
    },
    "run_doctor": {
        "module": "admin",
        "attr": "run_doctor",
        "group": "daemon",
        "description": "Run environment diagnostics and return the report text.",
        "schema": _schema({}),
    },
    # ----------------------------------------------------------- recording ---
    "start_recording": {
        "module": "recording",
        "attr": "start_recording",
        "group": "recording",
        "description": "Begin an action trace: per-action JPEG screenshots plus events.jsonl. Works for /tools, /batch and /run; returns a directory (not MP4).",
        "schema": _schema(
            {
                "name": _prop("string", "Recording name."),
                "title": _prop("string", "Human-readable title."),
            }
        ),
    },
    "stop_recording": {
        "module": "recorder",
        "attr": "stop_recording",
        "group": "recording",
        "description": "Stop the active recording.",
        "schema": _schema({}),
    },
    "recordings": {
        "module": "recorder",
        "attr": "recordings",
        "group": "recording",
        "description": "List recorded sessions, newest first.",
        "schema": _schema({}),
    },
    "latest_recording": {
        "module": "recorder",
        "attr": "latest_recording",
        "group": "recording",
        "description": "Path of the most recent recording.",
        "schema": _schema({}),
    },
    "recording_dir": {
        "module": "recorder",
        "attr": "recording_dir",
        "group": "recording",
        "description": "Directory of the currently active recording, if any.",
        "schema": _schema({}),
    },
    "set_auto_recording": {
        "module": "recorder",
        "attr": "set_auto_recording",
        "group": "recording",
        "description": "Turn automatic background recording on or off.",
        "schema": _schema({"enabled": _prop("boolean", "Enable auto-recording.", required=True)}, ["enabled"]),
    },
    "auto_recording_setting": {
        "module": "recorder",
        "attr": "auto_recording_setting",
        "group": "recording",
        "description": "Current auto-recording preference and where it came from.",
        "schema": _schema({}),
    },
}


def _resolve(module_name: str, attr: str) -> Callable[..., Any]:
    """Import a helper lazily so the registry stays importable without a browser."""
    if module_name == "helpers":
        from browser_harness import helpers as mod
    elif module_name == "local":
        from . import interactions as mod
    elif module_name == "admin":
        from browser_harness import admin as mod
    elif module_name == "recording":
        from . import recording as mod
    elif module_name == "recorder":
        from browser_harness import recorder as mod
    else:  # pragma: no cover - guards against typos in TOOL_SPECS
        raise KeyError(f"unknown module {module_name!r}")
    return getattr(mod, attr)


def get_tool(name: str) -> Callable[..., Any]:
    spec = TOOL_SPECS[name]
    return _resolve(spec["module"], spec["attr"])


def describe_tools() -> list[dict[str, Any]]:
    """Self-describing catalogue used by GET /tools."""
    out = []
    for name, spec in TOOL_SPECS.items():
        try:
            fn = _resolve(spec["module"], spec["attr"])
            signature = str(inspect.signature(fn))
        except Exception as exc:  # pragma: no cover - only on a broken install
            signature = f"<unavailable: {exc}>"
        out.append(
            {
                "name": name,
                "group": spec["group"],
                "description": spec["description"],
                "signature": f"{name}{signature}",
                "input_schema": spec["schema"],
            }
        )
    return out


def call_tool(name: str, args: dict[str, Any]) -> Any:
    """Invoke a registered tool with JSON arguments."""
    if name not in TOOL_SPECS:
        raise KeyError(name)
    fn = get_tool(name)
    kwargs = dict(args or {})
    if name == "cdp":
        method = kwargs.pop("method")
        session_id = kwargs.pop("session_id", None)
        params = kwargs.pop("params", {}) or {}
        return fn(method, session_id=session_id, **params)
    allowed = set(inspect.signature(fn).parameters)
    unknown = set(kwargs) - allowed
    if unknown:
        raise TypeError(f"unexpected argument(s) for {name}: {', '.join(sorted(unknown))}")
    return fn(**kwargs)


GROUPS: dict[str, list[str]] = {}
for _name, _spec in TOOL_SPECS.items():
    GROUPS.setdefault(_spec["group"], []).append(_name)
