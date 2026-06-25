"""harness-lens daemon — a single local control plane for harness hook events.

The original design wired each harness hook to a short-lived ``harness-lens hook``
process that *always exits 0* (observe-only). This package replaces that with a
single long-running daemon (FastAPI on ``127.0.0.1:7700``) that:

* normalises Claude Code **and** Codex hook payloads into one :class:`HarnessEvent`
  (:mod:`harness_lens.daemon.events`),
* evaluates each control event through the 3-Layer policy engine
  (:mod:`harness_lens.daemon.policy`) and may return **deny / escalate / allow**
  rather than the old always-allow,
* serialises every write into the Flow/Task/Step ledger through a single writer
  (:mod:`harness_lens.daemon.ledger` + :mod:`harness_lens.daemon.writer`),
* and parks ``escalate`` decisions in an approval queue
  (:mod:`harness_lens.daemon.approvals`) until a human (GUI or terminal) resolves
  them.

The daemon binds loopback only and authenticates hook→daemon requests with a
per-install token (:func:`harness_lens.daemon.config.ensure_token`). Heavy deps
(FastAPI/uvicorn/websockets/watchdog) live in the ``harness-lens[daemon]`` extra,
so existing observe-only installs are untouched until a user opts in.
"""

from __future__ import annotations

import os

# Network surface. Loopback only — never bind a routable interface (design constraint). The port is
# overridable (``HARNESS_LENS_DAEMON_PORT``) for the rare :7700 conflict; the host stays loopback-only.
DAEMON_HOST = "127.0.0.1"


def _resolve_port() -> int:
    raw = os.environ.get("HARNESS_LENS_DAEMON_PORT", "").strip()
    try:
        return int(raw) if raw else 7700
    except ValueError:
        return 7700


DAEMON_PORT = _resolve_port()


def daemon_base_url() -> str:
    return f"http://{DAEMON_HOST}:{DAEMON_PORT}"


__all__ = ["DAEMON_HOST", "DAEMON_PORT", "daemon_base_url"]
