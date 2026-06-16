"""Hook-side relay — the thin process a harness hook invokes to reach the daemon.

Installed hooks call ``harness-lens hook-relay <source>``. This reads the hook payload from
stdin, POSTs it to the daemon, and relays the daemon's JSON response to stdout (which the
harness interprets as the hook's allow/deny/ask output). It is uniform across Claude Code and
Codex — the daemon's adapter renders the right per-harness response shape.

**Fail-open by default**: if the daemon is unreachable, a control hook allows the action and
appends a line to ``hook-fallback.log`` rather than blocking the agent (design). Set
``fail_open=false`` in ``daemon.json`` for fail-closed (deny on outage).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .. import home_dir
from . import daemon_base_url
from .config import DaemonConfig, ensure_token, fallback_log_path

# Native hook event names that can *block* — these justify waiting through an approval, and
# fail-closed denies them; everything else is observe-only and always allowed on outage.
_CONTROL_HOOK_NAMES = {"PreToolUse", "Stop", "SubagentStop", "UserPromptSubmit"}


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _hook_name(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or payload.get("event") or "")


def relay(source: str, root: Optional[Path] = None) -> int:
    root = root or home_dir()
    payload = _read_stdin()
    config = DaemonConfig.load(root)
    token = ensure_token(root)
    is_control = _hook_name(payload) in _CONTROL_HOOK_NAMES
    # A control event may block on a human approval; allow the full approval window plus slack.
    timeout = (config.approval_timeout_sec + 10.0) if is_control else 5.0

    req = urllib.request.Request(
        f"{daemon_base_url()}/hook/{source}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-HL-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        sys.stdout.write(body or "{}")
        return 0
    except (urllib.error.URLError, OSError) as exc:
        return _on_outage(source, payload, config, is_control, exc, root)


def _on_outage(source: str, payload: dict, config: DaemonConfig, is_control: bool,
               exc: Exception, root: Path) -> int:
    _log_fallback(root, source, payload, str(exc))
    if config.fail_open or not is_control:
        sys.stdout.write("{}")  # empty output = allow / no-op for both harnesses
        return 0
    # Fail-closed: deny the control action with a clear reason in the source's shape.
    reason = "harness-lens daemon 미응답 (fail-closed)"
    hook = _hook_name(payload) or "PreToolUse"
    if source == "codex" and hook != "PreToolUse":
        # Codex Stop/UserPromptSubmit block via the top-level `decision` (no hookSpecificOutput wire).
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    else:
        # Both Claude Code and Codex ≥0.139 share this PreToolUse shape — allow/deny/ask live in
        # hookSpecificOutput.permissionDecision (Codex's top-level `decision` only accepts approve/block,
        # so the legacy {"decision":"deny"} produced "invalid pre-tool-use JSON output").
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": hook, "permissionDecision": "deny", "permissionDecisionReason": reason,
        }}))
    return 0


def _log_fallback(root: Path, source: str, payload: dict, error: str) -> None:
    try:
        line = json.dumps({
            "ts": time.time(), "source": source, "hook": _hook_name(payload),
            "session_id": payload.get("session_id") or payload.get("sessionId"),
            "error": error,
        }, ensure_ascii=False)
        with open(fallback_log_path(root), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    source = argv[0] if argv else ""
    if source not in ("claude_code", "codex"):
        # Unknown source: never block the agent.
        sys.stdout.write("{}")
        return 0
    return relay(source)


if __name__ == "__main__":
    raise SystemExit(main())
