"""API-side interaction helpers layered on top of browser_harness helpers.

These exist because a raw HTTP caller can hit the browser before the page is
ready, or fight the page's own smooth scrolling when clicking. They are kept
importable without a browser by importing browser_harness lazily.
"""

from __future__ import annotations

import json
import time
from typing import Any


def _helpers():
    from browser_harness import helpers

    return helpers


def wait_for_document(timeout: float = 5.0) -> bool:
    """Poll until the tab has a document element (page at least parsed)."""
    helpers = _helpers()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if helpers.js("!!document.documentElement && document.readyState !== 'loading'"):
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def guarded_page_info() -> Any:
    """page_info that waits for a parsed document instead of crashing.

    A bare page_info right after new_tab races the navigation: the upstream
    helper reads document.documentElement.scrollWidth and dies with
    "Cannot read properties of null" when the document is not there yet.
    """
    wait_for_document(timeout=5.0)
    return _helpers().page_info()


def click_selector(selector: str, timeout: float = 10.0, button: str = "left", click_timeout: float = 3.0) -> dict:
    """Click a CSS-selected element with a real CDP mouse click.

    Polls for the element to exist (every 100ms, not the strict visibility
    check that can burn the whole timeout on an element that is present but
    mid-transition), then in a bounded retry loop scrolls it into view
    instantly - never smooth, a mid-scroll click lands on whatever is passing
    by - and verifies it is the actual hit target.

    The click is dispatched ONLY on a true hit. When another element covers
    the target (cookie bar, promo overlay), no click is sent: the result
    reports hit_target=False and a `blocked_by` description of the covering
    element, so the caller can dismiss the blocker and retry instead of
    clicking whatever happened to be on top.
    """
    helpers = _helpers()
    sel = json.dumps(selector)
    deadline = time.time() + timeout
    exists = False
    while time.time() < deadline:
        try:
            if helpers.js(f"!!document.querySelector({sel})"):
                exists = True
                break
        except Exception:
            pass
        time.sleep(0.1)
    if not exists:
        raise RuntimeError(f"element not found within {timeout:.1f}s: {selector}")

    hit_deadline = time.time() + click_timeout
    rect = None
    blocker = None
    while True:
        rect = helpers.js(
            "(() => {"
            f"  const el = document.querySelector({sel});"
            "  if (!el) return null;"
            "  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});"
            "  const r = el.getBoundingClientRect();"
            "  return {x: r.left + r.width / 2, y: r.top + r.height / 2,"
            "          width: r.width, height: r.height};"
            "})()"
        )
        if rect:
            x, y = rect["x"], rect["y"]
            probe = helpers.js(
                "(() => {"
                f"  const el = document.querySelector({sel});"
                f"  const top = document.elementFromPoint({x!r}, {y!r});"
                "  if (!el || !top) return {hit: false, top: null};"
                "  const hit = top === el || el.contains(top) || top.contains(el);"
                "  const cls = typeof top.className === 'string' ? top.className.trim() : '';"
                "  const desc = top.tagName.toLowerCase()"
                "    + (top.id ? '#' + top.id : '')"
                "    + (cls ? '.' + cls.split(/\\s+/).slice(0, 3).join('.') : '');"
                "  return {hit: hit, top: hit ? null : desc};"
                "})()"
            ) or {}
            if probe.get("hit"):
                helpers.click_at_xy(x, y, button=button)
                return {"clicked": selector, "x": x, "y": y, "hit_target": True}
            blocker = probe.get("top")
        if time.time() >= hit_deadline:
            break
        time.sleep(0.15)
    return {
        "clicked": None,
        "selector": selector,
        "x": rect.get("x") if rect else None,
        "y": rect.get("y") if rect else None,
        "hit_target": False,
        "blocked_by": blocker,
        "note": "no click dispatched: target is covered or not hittable; dismiss the blocker and retry",
    }
