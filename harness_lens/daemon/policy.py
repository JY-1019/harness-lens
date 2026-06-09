"""Policy engine — evaluate a control event through the 3 layers (no LLM in this path).

PreToolUse pipeline (order fixed by design):

1. **L1 Invariant** — deterministic regex/structural match (the existing
   :class:`~harness_lens.criteria.invariant.InvariantChecker`). A violation denies in
   enforce mode; in observe mode it is recorded but allowed. No LLM, synchronous.
2. **L2 Domain** — in enforce mode a *suspected* violation escalates (parks in the
   approval queue). The control path may not call the LLM, so "suspected" is a fast
   structural pre-check that mirrors the DC-001 domain criterion; the real Judge runs
   later on the async path. In observe mode L2 never blocks.
3. **L3 QA** — never blocks here; recorded asynchronously only.

Stop / SubagentStop: in enforce mode, if ``completion_criteria`` (a new, optional
``criteria.yaml`` section) is unmet, block to force the agent to continue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from ..criteria import ThreeLayerCriteria
from .capabilities import Decision
from .config import MODE_ENFORCE
from .events import HarnessEvent

# Tools that write/modify a file — the DC-001 "read before edit" structural check applies.
_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"}
_READ_TOOLS = {"Read", "open", "cat"}


@dataclass
class _SyntheticStep:
    """Minimal step shape the InvariantChecker reads (tool_name + input/output text)."""

    tool_name: str
    input_summary: str = ""
    output_summary: str = ""


@dataclass
class PolicyContext:
    """Per-flow facts the policy needs but cannot derive from a single event.

    The daemon assembles this from the ledger before evaluating, keeping the engine pure
    (no DB coupling) and trivially unit-testable.
    """

    read_paths: set[str] = field(default_factory=set)  # file paths Read so far in this flow
    failed_steps: int = 0
    total_steps: int = 0


class PolicyEngine:
    def __init__(self, criteria: ThreeLayerCriteria, criteria_path: Optional[Path] = None):
        self.criteria = criteria
        self.invariant = criteria.invariant_checker()
        self.completion_criteria = _load_completion_criteria(criteria_path)

    # -- PreToolUse ------------------------------------------------------ #
    def evaluate_pre_tool(
        self, event: HarnessEvent, mode: str, context: Optional[PolicyContext] = None
    ) -> Decision:
        context = context or PolicyContext()

        # L1 — deterministic invariant check. No output yet, so check name + input text.
        step = _SyntheticStep(tool_name=event.tool_name or "", input_summary=event.tool_text())
        passed, violations = self.invariant.check(step)
        if not passed:
            detail = "; ".join(f"{v.rule} ({v.detail})" for v in violations)
            if mode == MODE_ENFORCE:
                return Decision.deny(layer=1, reason=f"Layer 1 invariant 위반 — {detail}")
            # observe: record the violation but never block (upgrade-safe default behaviour).
            return Decision.allow(layer=1, reason=f"Layer 1 위반(observe, 미차단) — {detail}")

        # L2 — enforce-only suspected-violation escalation (deterministic pre-check, no LLM).
        if mode == MODE_ENFORCE:
            suspicion = self._l2_suspect(event, context)
            if suspicion is not None:
                return Decision.escalate(layer=2, reason=suspicion)

        # L3 — never blocks in the control path; the async path records thresholds.
        return Decision.allow()

    def _l2_suspect(self, event: HarnessEvent, context: PolicyContext) -> Optional[str]:
        """A fast structural analogue of DC-001 ("read a file before editing it").

        Editing a file with no prior Read of that same path in the flow is the suspicion the
        LLM Judge would investigate; we surface it for human escalation without an LLM call.
        Returns a reason string when suspect, else None.
        """
        tool = event.tool_name or ""
        if tool not in _EDIT_TOOLS:
            return None
        path = _edit_path(event)
        if path is None:
            return None
        if path in context.read_paths:
            return None
        return f"[DC-001] {tool} {path} — 수정 전 해당 파일 Read 기록이 없음 (검토 필요)"

    # -- Stop / SubagentStop --------------------------------------------- #
    def evaluate_stop(
        self, event: HarnessEvent, mode: str, context: Optional[PolicyContext] = None
    ) -> Decision:
        if mode != MODE_ENFORCE or not self.completion_criteria:
            return Decision.allow()
        context = context or PolicyContext()
        unmet = [c["id"] for c in self.completion_criteria if not _completion_met(c, context)]
        if unmet:
            return Decision.deny(
                layer=None,
                reason="완료 기준 미충족: " + ", ".join(unmet) + " — 작업을 계속하세요.",
            )
        return Decision.allow()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _edit_path(event: HarnessEvent) -> Optional[str]:
    inp = event.tool_input or {}
    for key in ("file_path", "path", "filePath", "notebook_path"):
        value = inp.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def read_path_of(event: HarnessEvent) -> Optional[str]:
    """The file path a Read-like step touched (used by the daemon to build PolicyContext)."""
    if (event.tool_name or "") not in _READ_TOOLS:
        return None
    return _edit_path(event)


def _load_completion_criteria(criteria_path: Optional[Path]) -> list[dict]:
    """Parse the optional ``completion_criteria`` section from criteria.yaml.

    Each entry is ``{id, description, check}`` where ``check`` is a small built-in keyword
    (``no_failed_steps`` | ``min_steps:N``). Unknown checks are treated as always-met so a
    typo never wedges a session in an un-stoppable loop. Absent section → no completion gate
    (default), so Stop is never blocked unless the user opts in.
    """
    if criteria_path is None or not Path(criteria_path).exists():
        return []
    try:
        data = yaml.safe_load(Path(criteria_path).read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return []
    raw = data.get("completion_criteria") or []
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            out.append({"id": f"CC-{i+1:03d}", "description": entry, "check": entry})
        elif isinstance(entry, dict) and entry.get("id"):
            out.append({
                "id": str(entry["id"]),
                "description": str(entry.get("description", "")),
                "check": str(entry.get("check", "")),
            })
    return out


def _completion_met(criterion: dict, context: PolicyContext) -> bool:
    check = (criterion.get("check") or "").strip()
    if check == "no_failed_steps":
        return context.failed_steps == 0
    if check.startswith("min_steps:"):
        try:
            need = int(check.split(":", 1)[1])
        except ValueError:
            return True
        return context.total_steps >= need
    # Unknown / free-text criterion: not deterministically checkable here — treat as met so we
    # never block forever on something the control path cannot evaluate without an LLM.
    return True
