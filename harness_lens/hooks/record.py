"""Hook receiver.

Invoked once per harness event as ``harness-lens hook <event>`` with the hook's
JSON payload on stdin. It reconstructs Flow/Task/Step into the ledger and **always
exits 0** — harness-lens observes, it never blocks the agent.

Layer-2 (the LLM Judge) is *not* run inline by default: hooks have tight timeouts
and must never stall the agent. Layer 1 (deterministic) always runs. Set
``HARNESS_LENS_JUDGE_IN_HOOK=1`` (with an API key) to opt into inline sampling.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from .. import home_dir
from ..criteria import CriteriaEngine, ThreeLayerCriteria
from ..reconstructor import Reconstructor
from ..store import SQLiteStore

EVENTS = ("session-start", "user-prompt", "pre-tool", "post-tool", "post-tool-failure", "stop", "session-end")

_SUMMARY_LIMIT = 800


def _summarize(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:_SUMMARY_LIMIT]


# PostToolUse names its result payload differently across sources: the Anthropic hooks
# reference documents `tool_response`, while the installed Claude Code plugin-dev/hookify
# skills document `tool_result`. Read whichever is present so output capture and failure
# inference work regardless of which the running Claude Code build emits.
def _tool_result(payload: dict):
    value = payload.get("tool_response")
    return value if value is not None else payload.get("tool_result")


def _infer_success(payload: dict) -> bool:
    response = _tool_result(payload)
    if isinstance(response, dict):
        if response.get("error") or response.get("is_error"):
            return False
        if response.get("success") is False:
            return False
    if payload.get("success") is False:
        return False
    if isinstance(payload.get("error"), (str, dict)) and payload.get("error"):
        return False
    return True


def _read_payload(stream=sys.stdin) -> dict:
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _build_reconstructor(judge_in_hook: bool) -> Reconstructor:
    store = SQLiteStore()
    criteria = ThreeLayerCriteria.load(home_dir() / "criteria.yaml")
    llm = None
    if judge_in_hook:
        from ..llm import default_client

        llm = default_client()
    engine = CriteriaEngine(criteria, store, llm=llm)
    return Reconstructor(store, engine)


def handle_event(event: str, payload: dict, reconstructor: Optional[Reconstructor] = None) -> None:
    if reconstructor is None:
        judge_in_hook = os.environ.get("HARNESS_LENS_JUDGE_IN_HOOK", "") == "1"
        reconstructor = _build_reconstructor(judge_in_hook)

    session_id = str(payload.get("session_id") or payload.get("sessionId") or "unknown")
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")

    if event == "session-start":
        reconstructor.on_session_start(session_id, platform=payload.get("platform"))
    elif event == "user-prompt":
        # `prompt` per the Anthropic hooks reference; `user_prompt` per the installed
        # plugin-dev/hookify skills. Accept either so the task name is never lost.
        prompt = payload.get("prompt")
        if prompt is None:
            prompt = payload.get("user_prompt", "")
        reconstructor.on_user_prompt(session_id, str(prompt))
    elif event == "pre-tool":
        reconstructor.on_pre_tool(session_id, tool_name, _summarize(payload.get("tool_input")))
    elif event in ("post-tool", "post-tool-failure"):
        # PostToolUseFailure is always a failure; PostToolUse infers from the payload.
        success = False if event == "post-tool-failure" else _infer_success(payload)
        # PostToolUseFailure delivers the reason in the top-level `error`, not the tool
        # result, so fall back to it — otherwise every failed step records an empty
        # output_summary and Tier-3 evidence / the Debugger see only success=False.
        output = _tool_result(payload)
        if not output:
            output = payload.get("error")
        reconstructor.on_post_tool(
            session_id, tool_name,
            output_summary=_summarize(output),
            success=success,
            latency_ms=payload.get("duration_ms", payload.get("latency_ms")),
            # Forwarded so a step synthesized here (missed/timed-out PreToolUse) still
            # captures the tool input the Debugger needs, instead of an empty summary.
            input_summary=_summarize(payload.get("tool_input")),
        )
    elif event == "stop":
        reconstructor.on_stop(session_id)
    elif event == "session-end":
        reconstructor.on_session_end(
            session_id,
            total_tokens=int(payload.get("total_tokens", 0) or 0),
            status=payload.get("status"),
        )


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    event = argv[0] if argv else ""
    if event not in EVENTS:
        # Unknown event: stay silent, never block the agent.
        return 0
    try:
        handle_event(event, _read_payload())
    except Exception as exc:  # noqa: BLE001 — observation must never raise into the agent
        print(f"harness-lens hook error ({event}): {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
