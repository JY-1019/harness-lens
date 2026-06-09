"""Daemon runtime config + on-disk locations + auth token.

Everything the daemon persists lives under :func:`harness_lens.home_dir` (default
``~/.harness-lens``) so it sits alongside ``ledger.db`` / ``criteria.yaml``:

* ``daemon.json``        — :class:`DaemonConfig` (mode, fail-open, approval policy).
* ``token``             — the shared secret hook requests must present (chmod 600).
* ``daemon.pid``        — the running daemon's pid (for ``daemon stop/status``).
* ``daemon.sock``       — optional unix socket path (reserved; HTTP is primary).
* ``hook-fallback.log`` — where a hook writes when the daemon is unreachable
                          (fail-open audit trail).

``mode`` is deliberately stored here rather than in ``criteria.yaml``: it is an
operational toggle (observe vs enforce), not a criterion, and must be switchable
at runtime (``harness-lens mode ...``) without rewriting the layers.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import home_dir

# Modes. ``observe`` records and (asynchronously) judges but never blocks — this is the
# default so an existing observe-only install behaves identically after upgrade. ``enforce``
# additionally denies Layer-1 violations and escalates suspected Layer-2 violations.
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"
MODES = (MODE_OBSERVE, MODE_ENFORCE)

# What to do when an escalate has no human answer within ``approval_timeout_sec``.
TIMEOUT_DENY = "deny"
TIMEOUT_ALLOW = "allow"
TIMEOUT_ESCALATE_TERMINAL = "escalate_to_terminal"
TIMEOUT_POLICIES = (TIMEOUT_DENY, TIMEOUT_ALLOW, TIMEOUT_ESCALATE_TERMINAL)


def config_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "daemon.json"


def token_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "token"


def pid_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "daemon.pid"


def socket_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "daemon.sock"


def fallback_log_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "hook-fallback.log"


def ensure_token(root: Optional[Path] = None) -> str:
    """Return the install's hook auth token, creating it (0600) on first use.

    The token gates every hook→daemon request. Because the daemon binds loopback only,
    this primarily stops *other local users* on a shared machine from injecting events or
    reading the control plane; it is generated once and reused across daemon restarts.
    """
    path = token_path(root)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort: a filesystem without POSIX perms (e.g. some mounts) still gets the
        # loopback bind + token; we just cannot tighten the mode.
        pass
    return token


@dataclass
class DaemonConfig:
    """Operational policy for the running daemon, persisted to ``daemon.json``."""

    mode: str = MODE_OBSERVE
    # When the daemon is unreachable, a control hook allows the action and logs locally
    # (fail-open) rather than blocking the agent. Set False for fail-closed (deny on outage).
    fail_open: bool = True
    # How long a hook request blocks waiting for a human to resolve an escalate.
    approval_timeout_sec: float = 60.0
    # Applied when that wait elapses with no answer.
    default_on_timeout: str = TIMEOUT_DENY
    # Layer-2 sample rate for the asynchronous Judge path (the control path never calls the LLM).
    judge_sample_rate: float = 0.2
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            self.mode = MODE_OBSERVE
        if self.default_on_timeout not in TIMEOUT_POLICIES:
            self.default_on_timeout = TIMEOUT_DENY

    # -- persistence ----------------------------------------------------- #
    @classmethod
    def load(cls, root: Optional[Path] = None) -> "DaemonConfig":
        path = config_path(root)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt config must not crash the daemon at startup — fall back to safe
            # defaults (observe + fail-open) rather than refuse to run.
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(**kwargs, extra=extra)

    def save(self, root: Optional[Path] = None) -> Path:
        path = config_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": self.mode,
            "fail_open": self.fail_open,
            "approval_timeout_sec": self.approval_timeout_sec,
            "default_on_timeout": self.default_on_timeout,
            "judge_sample_rate": self.judge_sample_rate,
            **self.extra,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "fail_open": self.fail_open,
            "approval_timeout_sec": self.approval_timeout_sec,
            "default_on_timeout": self.default_on_timeout,
            "judge_sample_rate": self.judge_sample_rate,
        }
