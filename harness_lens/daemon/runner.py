"""Daemon process management — ``daemon start | stop | status``.

``serve`` runs uvicorn in the foreground (this is what the spawned background process and a
``--foreground`` invocation both call). ``start`` double-forks a detached ``python -m
harness_lens.daemon`` and writes a pidfile; ``stop`` SIGTERMs it; ``status`` probes the live
HTTP endpoint with the auth token.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import home_dir
from . import DAEMON_HOST, DAEMON_PORT, daemon_base_url
from .config import ensure_token, pid_path


def serve(host: str = DAEMON_HOST, port: int = DAEMON_PORT, root: Optional[Path] = None) -> int:
    """Run the daemon in the foreground until interrupted (blocking)."""
    import uvicorn

    from .app import create_app

    app = create_app(root=root)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _read_pid(root: Optional[Path] = None) -> Optional[int]:
    path = pid_path(root)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_running(root: Optional[Path] = None) -> bool:
    pid = _read_pid(root)
    return bool(pid and _alive(pid))


def start(root: Optional[Path] = None) -> dict:
    root = root or home_dir()
    root.mkdir(parents=True, exist_ok=True)
    if is_running(root):
        return {"status": "already-running", "pid": _read_pid(root)}
    ensure_token(root)
    log = open(root / "daemon.out.log", "ab")
    env = dict(os.environ)
    # Spawn a detached daemon process. start_new_session so it survives the parent shell.
    proc = subprocess.Popen(
        [sys.executable, "-m", "harness_lens.daemon"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    pid_path(root).write_text(str(proc.pid), encoding="utf-8")
    # Give uvicorn a moment to bind, then confirm via the status probe.
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if status(root).get("ok"):
            return {"status": "started", "pid": proc.pid}
        if proc.poll() is not None:
            return {"status": "failed", "pid": proc.pid, "detail": "process exited; see daemon.out.log"}
        time.sleep(0.25)
    return {"status": "starting", "pid": proc.pid, "detail": "not yet responding; check status"}


def stop(root: Optional[Path] = None) -> dict:
    pid = _read_pid(root)
    if not pid or not _alive(pid):
        pid_path(root).unlink(missing_ok=True)
        return {"status": "not-running"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 6.0
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)
    pid_path(root).unlink(missing_ok=True)
    return {"status": "stopped", "pid": pid}


def status(root: Optional[Path] = None) -> dict:
    root = root or home_dir()
    token = ensure_token(root)
    req = urllib.request.Request(
        f"{daemon_base_url()}/api/status", headers={"X-HL-Token": token}
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "pid": _read_pid(root), **payload}
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {"ok": False, "pid": _read_pid(root), "running": is_running(root)}
