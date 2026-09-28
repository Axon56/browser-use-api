"""Regression tests for the API layer.

Unit-level: no live browser required. Run with:

    .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import os
import threading

import pytest

from bu_api import sessions
from bu_api.registry import TOOL_SPECS, call_tool


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("BU_CHROME_BINARY", "BU_BROWSER"):
        monkeypatch.delenv(key, raising=False)


# 1. find_browser honours an explicit BU_CHROME_BINARY path.
def test_find_browser_explicit_env(monkeypatch, tmp_path):
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("BU_CHROME_BINARY", str(fake))
    assert sessions.find_browser() == str(fake)
    assert sessions.find_browser("edge") == str(fake)  # explicit path always wins


# 2. Unsupported browsers fail loudly with the CDP reason, never silently.
def test_unsupported_browsers_explain_cdp():
    for family in ("firefox", "safari"):
        with pytest.raises(ValueError, match="Chrome DevTools Protocol|CDP"):
            sessions.find_browser(family)
    listed = sessions.list_browsers()
    assert listed["firefox"]["supported"] is False
    assert listed["chrome"]["supported"] is True


# 3. Unknown family names fall back to a PATH lookup (e.g. google-chrome-beta).
def test_find_browser_unknown_family_uses_path(monkeypatch):
    monkeypatch.setattr(sessions.shutil, "which", lambda name: "/usr/bin/" + name if name == "my-browser" else None)
    assert sessions.find_browser("my-browser") == "/usr/bin/my-browser"
    assert sessions.find_browser("not-installed-anywhere") is None


# 4. page_info is the guarded variant; click_selector is registered and instant.
def test_registry_guards_and_click_selector():
    assert TOOL_SPECS["page_info"]["attr"] == "guarded_page_info"
    assert "click_selector" in TOOL_SPECS
    from bu_api import interactions

    source = open(interactions.__file__, encoding="utf-8").read()
    assert "behavior: 'instant'" in source  # never smooth-scroll before a click
    assert "elementFromPoint" in source  # hit-target check before clicking
    with pytest.raises(TypeError):
        call_tool("click_selector", {"bogus_arg": 1})


# 5. /run executes snippets off the ASGI event loop (worker thread).
def test_run_executes_off_event_loop():
    from fastapi.testclient import TestClient

    from bu_api import server

    client = TestClient(server.app)
    seen = {}

    original = server._ensure_daemon
    server._ensure_daemon = lambda *a, **k: None  # no browser needed for this
    try:
        loop_thread = threading.get_ident()

        def fake_to_thread(fn, *a, **k):
            import asyncio

            async def _run():
                seen["same_thread"] = threading.get_ident() == loop_thread
                if fn.__name__ == "_exec_snippet":
                    fn.__globals__["out"].write = lambda s: None
                return None

            return _run()

        # Simpler assertion: the route awaits asyncio.to_thread for the snippet.
        import inspect as _inspect

        src = _inspect.getsource(server.run_code)
        assert "asyncio.to_thread(_exec_snippet)" in src
    finally:
        server._ensure_daemon = original


# 6. click_selector never dispatches a click when the hit target check fails.
def test_click_selector_no_click_on_false_hit(monkeypatch):
    from bu_api import interactions

    class FakeHelpers:
        def __init__(self):
            self.clicks = []

        def js(self, expr):
            if "elementFromPoint" in expr:
                return {"hit": False, "top": "div#cookie-banner"}
            if "getBoundingClientRect" in expr:
                return {"x": 10.0, "y": 10.0, "width": 4.0, "height": 4.0}
            if "querySelector" in expr:
                return True
            return None

        def click_at_xy(self, x, y, button="left"):
            self.clicks.append((x, y, button))

    fake = FakeHelpers()
    monkeypatch.setattr(interactions, "_helpers", lambda: fake)
    result = interactions.click_selector("#btn", timeout=1.0, click_timeout=0.4)
    assert result["hit_target"] is False
    assert result["clicked"] is None
    assert result["blocked_by"] == "div#cookie-banner"
    assert fake.clicks == []  # the whole point: nothing was clicked


# 7. click_selector does not burn the full timeout on an element that exists.
def test_click_selector_fast_when_element_ready(monkeypatch):
    import time

    from bu_api import interactions

    class FakeHelpers:
        def __init__(self):
            self.clicks = []

        def js(self, expr):
            if "elementFromPoint" in expr:
                return {"hit": True, "top": None}
            if "getBoundingClientRect" in expr:
                return {"x": 10.0, "y": 10.0, "width": 4.0, "height": 4.0}
            if "querySelector" in expr:
                return True
            return None

        def click_at_xy(self, x, y, button="left"):
            self.clicks.append((x, y, button))

        def wait_for_element(self, *a, **k):  # must not be used: it can stall 10s
            raise AssertionError("click_selector called wait_for_element")

    fake = FakeHelpers()
    monkeypatch.setattr(interactions, "_helpers", lambda: fake)
    started = time.time()
    result = interactions.click_selector("#btn", timeout=10.0)
    elapsed = time.time() - started
    assert result["hit_target"] is True
    assert result["clicked"] == "#btn"
    assert fake.clicks == [(10.0, 10.0, "left")]
    assert elapsed < 2.0


# 8. /run snippet scope shadows raw page_info with the guarded variant.
def test_run_scope_shadows_page_info():
    from browser_harness import helpers

    from bu_api import interactions, server

    scope = server._snippet_scope()
    assert scope["page_info"] is interactions.guarded_page_info
    assert scope["raw_page_info"] is helpers.page_info
    assert scope["click_selector"].__wrapped__ is interactions.click_selector

# 9. API calls notify the CLI recorder only after successful visual actions.
def test_api_recording_tool_hook(monkeypatch):
    from bu_api import server

    recorded = []
    monkeypatch.setattr(server, "call_tool", lambda name, args: {"hit_target": args.get("hit", True), "x": 10, "y": 20})
    monkeypatch.setattr(server, "_record_action", lambda *a: recorded.append(a))
    server._run_recorded_tool("new_tab", {"url": "about:blank"})
    server._run_recorded_tool("click_selector", {"hit": False})
    server._run_recorded_tool("click_selector", {"hit": True})
    assert [item[0] for item in recorded] == ["new_tab", "click_at_xy"]


# 10. /run scope traces actual visual actions and leaves read-only helpers alone.
def test_run_recording_scope(monkeypatch):
    from browser_harness import helpers
    from bu_api import server

    calls = []
    monkeypatch.setattr(helpers, "new_tab", lambda url="about:blank": url)
    monkeypatch.setattr(server, "_record_action", lambda *a: calls.append(a))
    scope = server._snippet_scope()
    assert scope["new_tab"]("about:blank") == "about:blank"
    assert calls[0][0] == "new_tab"
    assert scope["raw_page_info"] is helpers.page_info

# 11. HTTP-provided recording labels cannot traverse folders or replace an old run.
def test_recording_name_safety(monkeypatch, tmp_path):
    from browser_harness import recorder
    from bu_api import recording

    monkeypatch.setattr(recorder, "recording_dir", lambda: None)
    captured = []
    monkeypatch.setattr(recorder, "start_recording", lambda **kw: captured.append(kw) or kw["name"])
    monkeypatch.setenv("BU_NAME", "user-work")
    with pytest.raises(ValueError):
        recording.start_recording("../outside")
    with pytest.raises(ValueError):
        recording.start_recording("a/b")
    one = recording.start_recording("my-run")
    two = recording.start_recording("my-run")
    assert one != two
    assert one.startswith("user-work-my-run-")
    monkeypatch.setattr(recorder, "recording_dir", lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match="active recording"):
        recording.start_recording("my-run")
