"""Named browser sessions.

A session is a dedicated Chrome instance plus the browser-harness daemon attached
to it. Create one, then address it from every later call with ``session_id``.
The registry is written to disk so sessions survive an API restart.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

REGISTRY_VERSION = 1


def _root() -> Path:
    base = os.environ.get("BH_HOME") or os.environ.get("BROWSER_HARNESS_HOME")
    if base:
        home = Path(base).expanduser()
    elif os.environ.get("XDG_CONFIG_HOME"):
        home = Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "browser-harness"
    else:
        home = Path.home() / ".config" / "browser-harness"
    return home / "api-sessions"


def registry_path() -> Path:
    return _root() / "sessions.json"


@dataclass
class Session:
    """One browser the API is driving."""

    id: str
    cdp_url: str | None = None
    port: int | None = None
    profile_dir: str | None = None
    chrome_pid: int | None = None
    managed: bool = False  # True when this API launched the browser
    headless: bool = True
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    start_url: str | None = None

    def public(self) -> dict:
        data = asdict(self)
        data["chrome_running"] = chrome_alive(self)
        return data


# ------------------------------------------------------------------- helpers ---
def _load() -> dict[str, Session]:
    path = registry_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions: dict[str, Session] = {}
    for name, data in (raw.get("sessions") or {}).items():
        known = {f for f in Session.__dataclass_fields__}
        sessions[name] = Session(**{k: v for k, v in data.items() if k in known})
    return sessions


def _save(sessions: dict[str, Session]) -> None:
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": REGISTRY_VERSION,
        "sessions": {name: asdict(s) for name, s in sessions.items()},
    }
    tmp = registry_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(registry_path())


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome() -> str | None:
    """Locate a Chromium-family binary, preferring the one the harness knows."""
    explicit = os.environ.get("BU_CHROME_BINARY")
    if explicit and Path(explicit).is_file():
        return explicit
    try:
        from browser_harness.admin import _DEFAULT_LAUNCH

        candidates = _DEFAULT_LAUNCH[1]
    except Exception:
        candidates = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge")
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    for path in (
        "/home/ayovps/.local/opt/axonbrowser/google-chrome/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(path).is_file():
            return path
    return None


def devtools_ready(cdp_url: str | None, timeout: float = 30.0) -> bool:
    """Poll ``/json/version`` until Chrome answers."""
    if not cdp_url:
        return False
    url = cdp_url.rstrip("/") + "/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                data = json.loads(response.read())
            if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                return True
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.3)
    return False


def chrome_alive(session: Session) -> bool:
    if session.chrome_pid:
        try:
            os.kill(session.chrome_pid, 0)
            return True
        except OSError:
            pass
    return devtools_ready(session.cdp_url, timeout=0.4)


# ------------------------------------------------------------------ lifecycle ---
def list_sessions() -> list[dict]:
    return [s.public() for s in _load().values()]


def get_session(name: str) -> Session | None:
    return _load().get(name)


def touch(name: str) -> None:
    sessions = _load()
    if name in sessions:
        sessions[name].last_used_at = time.time()
        _save(sessions)


def create(
    name: str,
    cdp_url: str | None = None,
    headless: bool = True,
    start_url: str | None = None,
    profile_dir: str | None = None,
) -> Session:
    """Create a session: attach to ``cdp_url`` or launch a fresh Chrome."""
    sessions = _load()
    if name in sessions:
        existing = sessions[name]
        if chrome_alive(existing):
            raise ValueError(f"session '{name}' already exists")
        # Dead session under the same name: replace it.
        sessions.pop(name)

    session = Session(id=name, cdp_url=cdp_url, headless=headless, start_url=start_url)

    if cdp_url:
        if not devtools_ready(cdp_url, timeout=10.0):
            raise RuntimeError(f"no Chrome DevTools endpoint reachable at {cdp_url}")
    else:
        binary = find_chrome()
        if not binary:
            raise RuntimeError("no Chromium-family browser found; set BU_CHROME_BINARY")
        port = free_port()
        profile = Path(profile_dir).expanduser() if profile_dir else _root() / name / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-search-engine-choice-screen",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
            "--disable-breakpad",
            "--no-sandbox",
            "--disable-gpu",
        ]
        if headless:
            args.append("--headless=new")
        args.append(start_url or "about:blank")

        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        session.port = port
        session.cdp_url = f"http://127.0.0.1:{port}"
        session.profile_dir = str(profile)
        session.chrome_pid = process.pid
        session.managed = True

        if not devtools_ready(session.cdp_url, timeout=30.0):
            _kill_process_group(process.pid)
            raise RuntimeError("Chrome started but never exposed a DevTools endpoint")

    sessions[name] = session
    _save(sessions)
    return session


def _kill_process_group(pid: int | None) -> None:
    if not pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return


def delete(name: str, stop_browser: bool = True) -> dict:
    """Tear down the daemon for this session, and its browser if we launched it."""
    sessions = _load()
    session = sessions.pop(name, None)
    if session is None:
        raise KeyError(name)
    _save(sessions)

    daemon_stopped = False
    try:
        from browser_harness import admin

        if admin.daemon_alive(name):
            admin.restart_daemon(name)
            daemon_stopped = True
    except Exception:
        pass

    browser_stopped = False
    if stop_browser and session.managed:
        _kill_process_group(session.chrome_pid)
        browser_stopped = True

    return {
        "id": name,
        "daemon_stopped": daemon_stopped,
        "browser_stopped": browser_stopped,
        "profile_dir": session.profile_dir,
    }
