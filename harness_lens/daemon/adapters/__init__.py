"""Adapter layer — harness hook payloads ⇄ :class:`HarnessEvent` and decisions.

Each adapter owns two explicit, table-driven mappings for one harness:

* **inbound**: ``hook_event_name`` → normalized :data:`~harness_lens.daemon.events.Kind`,
  plus field extraction into a :class:`HarnessEvent`.
* **outbound**: a policy :class:`~harness_lens.daemon.capabilities.Decision` →
  the JSON the harness expects on the hook's stdout, *after* capability downgrade.

Keeping both directions table-driven (rather than scattered ``if source ==`` checks)
is the design's requirement that the capability matrix be expressed in code.
"""

from __future__ import annotations

from typing import Optional

from .base import Adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

_ADAPTERS: dict[str, Adapter] = {
    "claude_code": ClaudeCodeAdapter(),
    "codex": CodexAdapter(),
}


def get_adapter(source: str) -> Optional[Adapter]:
    return _ADAPTERS.get(source)


__all__ = ["Adapter", "ClaudeCodeAdapter", "CodexAdapter", "get_adapter"]
