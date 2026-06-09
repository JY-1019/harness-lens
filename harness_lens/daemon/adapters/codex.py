"""Codex CLI hook adapter.

Codex's hook surface is narrower than Claude Code's (design Codex §1): the lifecycle
events are SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop — there is
no SessionEnd, no PostToolUseFailure, and no SubagentStop. Its stdin JSON carries
``session_id``, ``turn_id`` and ``transcript_path``.

Codex cannot express ``ask``/escalate or rewrite tool input (capability matrix). The
approval queue still parks an escalate so a human can resolve it, but the *response* can
only be allow/deny — so an unresolved escalate that reaches rendering is downgraded to
deny, and the downgrade note is recorded.
"""

from __future__ import annotations

from typing import Optional

from ..capabilities import ALLOW, DENY, Decision
from ..events import HarnessEvent
from .base import Adapter, _as_dict, _first


class CodexAdapter(Adapter):
    source = "codex"

    EVENT_MAP = {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "Stop": "stop",
        # Codex may emit notifications; map if present, ignore otherwise.
        "Notification": "notification",
    }

    def to_event(self, payload: dict) -> Optional[HarnessEvent]:
        hook_name = str(_first(payload, "hook_event_name", "hookEventName", "event", default=""))
        kind = self.EVENT_MAP.get(hook_name)
        if kind is None:
            return None
        prompt = _first(payload, "prompt", "user_prompt", "input")
        return HarnessEvent(
            source=self.source,
            kind=kind,
            session_id=str(_first(payload, "session_id", "sessionId", default="unknown")),
            turn_id=_first(payload, "turn_id", "turnId"),
            tool_name=_first(payload, "tool_name", "toolName"),
            tool_use_id=_first(payload, "tool_use_id", "call_id", "toolUseId"),
            tool_input=_as_dict(_first(payload, "tool_input", "arguments", "toolInput")),
            tool_output=_as_dict(_first(payload, "tool_output", "output", "tool_result")),
            transcript_path=_first(payload, "transcript_path", "rollout_path", "transcriptPath"),
            cwd=_first(payload, "cwd", "workdir"),
            model=_first(payload, "model"),
            prompt=str(prompt) if prompt is not None else None,
            ts=_first(payload, "timestamp", default=None) or _now(),
            raw=payload,
        )

    def render(self, decision: Decision, event: HarnessEvent) -> dict:
        decision = self.finalize(decision, event)
        # Codex has no "ask": an escalate that survives to rendering (e.g. on timeout with
        # default_on_timeout=escalate_to_terminal) collapses to deny per the capability matrix.
        action = decision.action
        if action not in (ALLOW, DENY):
            decision.downgrades.append(
                f"escalate→deny: codex cannot escalate ({action} collapsed to deny)"
            )
            action = DENY
        out: dict = {"decision": action}
        if decision.reason:
            out["reason"] = decision.reason
        if decision.inject_context:
            out["additionalContext"] = decision.inject_context
        return out


def _now() -> float:
    import time

    return time.time()
