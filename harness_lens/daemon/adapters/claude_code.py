"""Claude Code hook adapter.

Inbound: Claude Code delivers a JSON object on the hook's stdin carrying
``hook_event_name``, ``session_id``, ``transcript_path``, ``cwd`` and (for tool events)
``tool_name`` / ``tool_input`` / ``tool_response``. We map ``hook_event_name`` to a
normalized kind through :data:`EVENT_MAP`.

Outbound: PreToolUse expects ``hookSpecificOutput.permissionDecision`` of
``allow`` / ``deny`` / ``ask`` (``ask`` == escalate-to-terminal) with a reason, and may
carry ``updatedInput`` and ``additionalContext``. Stop / SubagentStop block with
``{"decision": "block", "reason": ...}`` to force the agent to continue.
"""

from __future__ import annotations

from typing import Optional

from ..capabilities import ALLOW, DENY, ESCALATE, Decision
from ..events import HarnessEvent
from .base import Adapter, _as_dict, _first


class ClaudeCodeAdapter(Adapter):
    source = "claude_code"

    EVENT_MAP = {
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "UserPromptSubmit": "user_prompt",
        "PreToolUse": "pre_tool_use",
        "PostToolUse": "post_tool_use",
        "PostToolUseFailure": "post_tool_failure",
        "SubagentStart": "subagent_start",
        "SubagentStop": "subagent_stop",
        "Stop": "stop",
        "Notification": "notification",
        "PreCompact": "pre_compact",
    }

    def to_event(self, payload: dict) -> Optional[HarnessEvent]:
        hook_name = str(_first(payload, "hook_event_name", "hookEventName", default=""))
        kind = self.EVENT_MAP.get(hook_name)
        if kind is None:
            return None
        prompt = _first(payload, "prompt", "user_prompt")
        # PostToolUse names the result `tool_response` (Anthropic ref) or `tool_result`
        # (installed plugin-dev/hookify skills); accept either.
        tool_output = _as_dict(_first(payload, "tool_response", "tool_result"))
        return HarnessEvent(
            source=self.source,
            kind=kind,
            session_id=str(_first(payload, "session_id", "sessionId", default="unknown")),
            turn_id=_first(payload, "turn_id", "turnId"),
            parent_session_id=_first(payload, "parent_session_id", "parentSessionId"),
            tool_name=_first(payload, "tool_name", "toolName"),
            tool_use_id=_first(payload, "tool_use_id", "toolUseId"),
            tool_input=_as_dict(_first(payload, "tool_input", "toolInput")),
            tool_output=tool_output,
            transcript_path=_first(payload, "transcript_path", "transcriptPath"),
            cwd=payload.get("cwd"),
            model=_first(payload, "model"),
            prompt=str(prompt) if prompt is not None else None,
            agent_name=_first(payload, "agent_name", "subagent_type", "agentName"),
            status=_first(payload, "status"),
            total_tokens=_coerce_int(_first(payload, "total_tokens", "totalTokens")),
            ts=_first(payload, "timestamp", default=None) or _now(),
            raw=payload,
        )

    def render(self, decision: Decision, event: HarnessEvent) -> dict:
        decision = self.finalize(decision, event)
        if event.kind in ("stop", "subagent_stop"):
            return self._render_stop(decision, event)
        if event.kind == "user_prompt":
            return self._render_user_prompt(decision)
        return self._render_pre_tool(decision, event)

    def _render_user_prompt(self, decision: Decision) -> dict:
        # A UserPromptSubmit hook must NOT return a PreToolUse-shaped output — Claude Code rejects it
        # ("incorrect event name: expected 'UserPromptSubmit' but got 'PreToolUse'"). Allow is a no-op;
        # deny blocks the prompt; inject rides a UserPromptSubmit hookSpecificOutput.
        if decision.action == DENY:
            return {"decision": "block", "reason": decision.reason or ""}
        if decision.inject_context:
            return {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": decision.inject_context,
            }}
        return {}

    def _render_pre_tool(self, decision: Decision, event: HarnessEvent) -> dict:
        # allow → "allow"; deny → "deny"; escalate → "ask" (escalate to the terminal prompt).
        permission = {ALLOW: "allow", DENY: "deny", ESCALATE: "ask"}.get(decision.action, "allow")
        specific = {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": decision.reason or "",
        }
        if decision.updated_input is not None:
            specific["updatedInput"] = decision.updated_input
        out: dict = {"hookSpecificOutput": specific}
        if decision.inject_context:
            out["additionalContext"] = decision.inject_context
        return out

    def _render_stop(self, decision: Decision, event: HarnessEvent) -> dict:
        # Only a deny (completion criteria unmet) forces continuation; allow lets the stop stand.
        if decision.action == DENY:
            return {"decision": "block", "reason": decision.reason or "completion criteria unmet"}
        out: dict = {}
        if decision.inject_context:
            out["hookSpecificOutput"] = {
                "hookEventName": "SubagentStop" if event.kind == "subagent_stop" else "Stop",
                "additionalContext": decision.inject_context,
            }
        return out


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> float:
    import time

    return time.time()
