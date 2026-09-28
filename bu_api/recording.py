"""Safe API adapter for browser-harness action recordings."""
from __future__ import annotations

import os
import re
import uuid

from browser_harness import recorder

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def start_recording(name: str | None = None, title: str | None = None) -> str:
    """Start a fresh recording, scoped to this API browser session.

    Never let an HTTP supplied label become an arbitrary filesystem path or
    overwrite a previous recording; the upstream helper accepts both.
    """
    if name is not None and not _LABEL.fullmatch(name):
        raise ValueError("recording name must be 1-64 ASCII letters, numbers, '_' or '-'")
    if title is not None and (not isinstance(title, str) or len(title) > 200):
        raise ValueError("title must be text of at most 200 characters")
    if recorder.recording_dir() is not None:
        raise RuntimeError("this session already has an active recording")
    label = name or "recording"
    session = os.environ.get("BU_NAME", "default")
    safe_session = re.sub(r"[^A-Za-z0-9_-]", "_", session)[:48]
    unique = f"{safe_session}-{label}-{uuid.uuid4().hex[:8]}"
    return recorder.start_recording(name=unique, title=title)
