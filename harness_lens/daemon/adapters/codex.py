"""Codex CLI hook adapter.

Codex's lifecycle events are SessionStart / UserPromptSubmit / PreToolUse / PostToolUse /
Stop — there is no SessionEnd, no PostToolUseFailure, and no SubagentStop. Its stdin JSON
carries ``session_id``, ``turn_id`` and ``transcript_path``.

Outbound (Codex CLI ≥ 0.139): the hook-output schema is the same Claude-Code shape — a
PreToolUse returns ``hookSpecificOutput.permissionDecision`` of ``allow`` / ``deny`` /
``ask`` (``ask`` == escalate to Codex's own approval prompt) with a reason, and may carry
``updatedInput`` and ``additionalContext`` *inside* ``hookSpecificOutput`` (Codex has no
top-level ``additionalContext``). Stop blocks with the top-level ``{"decision": "block",
"reason": ...}`` to force the agent to continue. Emitting the legacy ``{"decision": action}``
shape is rejected by Codex with "invalid pre-tool-use JSON output", because PreToolUse's
top-level ``decision`` only accepts ``approve``/``block`` — the allow/deny/ask verb belongs
in ``hookSpecificOutput.permissionDecision``.
"""

from __future__ import annotations

from typing import Optional

from ..capabilities import ALLOW, DENY, ESCALATE, Decision
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
        if event.kind in ("stop", "subagent_stop"):
            return self._render_stop(decision)
        if event.kind == "user_prompt":
            return self._render_user_prompt(decision)
        return self._render_pre_tool(decision)

    def _render_pre_tool(self, decision: Decision) -> dict:
        # Codex's *runtime* validator (codex-cli ≥0.139) is stricter than its JSON schema: it accepts
        # ONLY permissionDecision "deny" — and only with a non-empty permissionDecisionReason. It
        # rejects permissionDecision "allow"/"ask" and top-level decision "approve"/continue/stopReason
        # ("PreToolUse hook returned unsupported permissionDecision:allow"). So: allow = empty output;
        # deny = permissionDecision deny + reason; escalate (survives to render only on timeout) has no
        # "ask" → it collapses to deny. updatedInput requires permissionDecision:allow → unusable, dropped.
        if decision.action in (DENY, ESCALATE):
            if decision.action == ESCALATE:
                decision.downgrades.append("escalate→deny: codex runtime has no 'ask' permissionDecision")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason or "harness-lens 정책에 의해 차단됨",
            }}
        # allow — empty = allow. additionalContext (if any) rides hookSpecificOutput without a verb.
        if decision.inject_context:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": decision.inject_context,
            }}
        return {}

    def _render_user_prompt(self, decision: Decision) -> dict:
        # UserPromptSubmit can only block (top-level decision) or inject context; allow is a no-op.
        if decision.action == DENY:
            return {"decision": "block", "reason": decision.reason or ""}
        if decision.inject_context:
            return {"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": decision.inject_context,
            }}
        return {}

    def _render_stop(self, decision: Decision) -> dict:
        # Only a deny (completion criteria unmet) forces continuation; allow lets the stop stand.
        # Codex has no Stop-specific output, so context injection rides the top-level systemMessage.
        if decision.action == DENY:
            return {"decision": "block", "reason": decision.reason or "completion criteria unmet"}
        if decision.inject_context:
            return {"systemMessage": decision.inject_context}
        return {}


def _now() -> float:
    import time

    return time.time()
