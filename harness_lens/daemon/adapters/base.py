"""Shared adapter contract + helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..capabilities import Decision, downgrade
from ..events import HarnessEvent


def _as_dict(value) -> Optional[dict]:
    """Coerce a hook field to a dict, or None. Tool input/output are sometimes a bare
    string or list depending on the tool; wrap those so the ledger column stays JSON-shaped."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": value}


def _first(payload: dict, *keys, default=None):
    """First present (non-None) value among ``keys`` — harnesses spell fields differently
    (``session_id`` vs ``sessionId``, ``tool_response`` vs ``tool_result``)."""
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


class Adapter(ABC):
    source: str

    # hook_event_name → normalized kind. Subclasses fill this in explicitly.
    EVENT_MAP: dict[str, str] = {}

    @abstractmethod
    def to_event(self, payload: dict) -> Optional[HarnessEvent]:
        """Normalize a hook stdin payload into a HarnessEvent, or None for an unknown event."""

    @abstractmethod
    def render(self, decision: Decision, event: HarnessEvent) -> dict:
        """Render the harness-specific hook response JSON for a (already evaluated) decision."""

    def finalize(self, decision: Decision, event: HarnessEvent) -> Decision:
        """Clamp a decision to this source's capabilities (records downgrade notes)."""
        return downgrade(decision, self.source)
