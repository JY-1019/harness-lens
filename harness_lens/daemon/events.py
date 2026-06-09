"""The unified event schema — :class:`HarnessEvent`.

Both Claude Code and Codex hook payloads are normalised into this one dataclass by
the adapters (:mod:`harness_lens.daemon.adapters`) so everything downstream — policy
engine, ledger, event bus — speaks a single vocabulary regardless of which harness
fired the event.

Secret masking lives here too: ``raw`` is preserved for audit/replay, but obvious
credential shapes inside ``tool_output`` (and the masked ``raw``) are redacted
*before* anything is written to the ledger (design constraint).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Optional

Source = Literal["claude_code", "codex"]

# The normalized event kinds. Both harnesses map their native hook names onto these.
Kind = Literal[
    "session_start",
    "session_end",
    "user_prompt",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_failure",
    "subagent_start",
    "subagent_stop",
    "stop",
    "notification",
    "pre_compact",
]

# Events that may *block* the agent and therefore require a synchronous policy decision.
# Everything else is observe-only (recorded asynchronously, never gates the agent).
CONTROL_KINDS: frozenset[str] = frozenset(
    {"pre_tool_use", "stop", "subagent_stop", "user_prompt"}
)


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


@dataclass
class HarnessEvent:
    source: Source
    kind: Kind
    session_id: str
    event_id: str = field(default_factory=new_event_id)
    turn_id: Optional[str] = None
    parent_session_id: Optional[str] = None  # set on subagent events → the parent Flow
    tool_name: Optional[str] = None
    tool_use_id: Optional[str] = None  # pre/post pairing key
    tool_input: Optional[dict] = None
    tool_output: Optional[dict] = None
    transcript_path: Optional[str] = None
    cwd: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None  # user_prompt text (Task title source)
    agent_name: Optional[str] = None  # subagent name on subagent_start/stop
    status: Optional[str] = None  # session_end status, if the harness reports one
    total_tokens: Optional[int] = None
    ts: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    @property
    def is_control(self) -> bool:
        return self.kind in CONTROL_KINDS

    def tool_text(self) -> str:
        """Flat text of name + input for deterministic (regex/structural) policy checks.

        The Layer-1 invariant detectors match over combined step text; at PreToolUse there is
        no output yet, so we build the text from the tool name and its input alone.
        """
        import json

        parts = [self.tool_name or ""]
        if self.tool_input is not None:
            try:
                parts.append(json.dumps(self.tool_input, ensure_ascii=False, default=str))
            except TypeError:
                parts.append(str(self.tool_input))
        return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Secret masking
# --------------------------------------------------------------------------- #
# Shapes that are credentials regardless of surrounding key. Mirrors the redaction the
# evolver path uses, kept standalone so the daemon does not import LensService.
_SECRET_TOKEN_RE = re.compile(
    r"(?:sk-[A-Za-z0-9-]{8,}"
    r"|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}"  # AWS access key id
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"  # JWT
)
# PEM private-key block header — redact the whole block, not just the line.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_SENSITIVE_KEY_HINTS = (
    "token", "secret", "password", "passwd", "apikey", "api_key",
    "api-key", "auth", "credential",
)
REDACTION_PLACEHOLDER = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    low = key.lower()
    return any(hint in low for hint in _SENSITIVE_KEY_HINTS)


def _mask_str(value: str) -> str:
    value = _PRIVATE_KEY_RE.sub(REDACTION_PLACEHOLDER, value)
    return _SECRET_TOKEN_RE.sub(REDACTION_PLACEHOLDER, value)


def mask_secrets(node):
    """Return a deep copy of ``node`` with obvious secrets redacted.

    Masks by *key name* (``...token``/``password``/...) and by *value shape*
    (API tokens, PEM private-key blocks). Pure (no in-place mutation) so the
    caller's ``raw`` reference is never silently altered.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                out[key] = REDACTION_PLACEHOLDER
            else:
                out[key] = mask_secrets(value)
        return out
    if isinstance(node, list):
        return [mask_secrets(v) for v in node]
    if isinstance(node, str):
        return _mask_str(node)
    return node
