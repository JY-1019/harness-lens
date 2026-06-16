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

import re
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

# -- Layer-2 structural detectors -------------------------------------------- #
# Like the L1 keyword→detector map, but for *process* domain criteria: a domain criterion whose
# wording matches a keyword is enforced deterministically (escalates) by the matching detector,
# which inspects the step text + flow context. (Value-level business rules — "refund > charge" — are
# not observable from a tool call and remain Judge-scored; these catch the structural/process ones.)
_TEST_CMD = re.compile(
    r"\b(pytest|jest|vitest|go test|cargo test|npm (run )?test|yarn test|pnpm test|mvn test|gradle test|ctest|tox|nox|rspec|phpunit)\b",
    re.I,
)
_CHANGE_CMD = re.compile(
    r"\b(git commit|git push|git merge|deploy|kubectl apply|terraform apply|helm (upgrade|install)|npm publish|make deploy)\b",
    re.I,
)
_SKIP_VERIFY = re.compile(r"(--no-verify|--no-gpg-sign|\[skip ci\]|\bskip[-_ ]ci\b|--force(?!-with-lease))", re.I)

# Charge/payment handling being authored (a code *action*, not a passing mention) …
_PAYMENT_ACTION = re.compile(
    r"(결제\s*(요청|처리|승인|생성|시도|확정)|카드\s*승인|거래\s*(생성|처리)|chargeCard|charge\s*\(|"
    r"capture[_\s-]?payment|create[_\s-]?(payment|charge)|process[_\s-]?payment|payment[_\s-]?intent|"
    r"authorize[_\s-]?payment|/charge\b|/payments?\b|def\s+\w*charge|debit[_\s-]?card)",
    re.I,
)
# … with a velocity / rate-limit guard present somewhere in the same edit.
_VELOCITY_GUARD = re.compile(
    r"(rate[_\s-]?limit|ratelimit|velocity|throttl|속도\s*제한|too[_\s-]?many[_\s-]?attempts|"
    r"cooldown|debounce|sliding[_\s-]?window|token[_\s-]?bucket|leaky[_\s-]?bucket|\b429\b|"
    r"max[_\s-]?attempts|attempts?[_\s-]?per|\.incr\(|seen[_\s-]?recently)",
    re.I,
)


def looks_like_test(text: str) -> bool:
    """Whether a step's text is running a test suite — used to remember that tests ran in a flow."""
    return bool(_TEST_CMD.search(text or ""))


def _l2_test_before_change(event: "HarnessEvent", context: "PolicyContext") -> "str | None":
    m = _CHANGE_CMD.search(event.tool_text())
    if m and not context.ran_tests:
        return f'테스트 실행 기록 없이 "{m.group(0)}" 시도 — 회귀 테스트 동반 필요'
    return None


def _l2_no_skip_verify(event: "HarnessEvent", context: "PolicyContext") -> "str | None":
    m = _SKIP_VERIFY.search(event.tool_text())
    if m:
        return f'검증 우회 "{m.group(0)}" 사용'
    return None


def _l2_payment_velocity(event: "HarnessEvent", context: "PolicyContext") -> "str | None":
    """Charge/payment code authored without a velocity / rate-limit guard.

    Honest boundary: a coding hook cannot observe the *running* system actually throttling repeat
    charges — that is runtime behaviour, invisible here. What IS observable is whether the payment
    code the agent is writing *contains* a rate-limit/velocity guard. So this gates that structural
    proxy: an edit that authors charge handling but ships no throttle construct escalates for review.
    """
    if (event.tool_name or "") not in _EDIT_TOOLS:
        return None
    text = event.tool_text()
    if _PAYMENT_ACTION.search(text) and not _VELOCITY_GUARD.search(text):
        return "결제/거래 코드에 속도 제한(velocity·rate-limit) 가드가 보이지 않음 — 동일 카드 단시간 다발 결제 제한 필요"
    return None


# (keyword in the criterion's description) → structural detector.
_L2_DETECTORS = (
    (re.compile(r"(회귀\s*테스트|테스트.*(동반|없이|후|선행)|test.*before|배포\s*전.*테스트)", re.I), _l2_test_before_change),
    (re.compile(r"(검증.*(건너|우회|생략)|--no-verify|skip[-_ ]?ci|우회)", re.I), _l2_no_skip_verify),
    (re.compile(r"(속도\s*제한|rate[_\s-]?limit|velocity|다발\s*결제|단시간.*결제|throttl)", re.I), _l2_payment_velocity),
)


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
    ran_tests: bool = False  # whether a test suite ran earlier in this flow (for test-before-change)


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
            cid = violations[0].rule if violations else None  # which invariant fired (for GUI highlight)
            if mode == MODE_ENFORCE:
                return Decision.deny(layer=1, reason=f"Layer 1 invariant 위반 — {detail}", criterion_id=cid)
            # observe: record the violation but never block (upgrade-safe default behaviour).
            return Decision.allow(layer=1, reason=f"Layer 1 위반(observe, 미차단) — {detail}", criterion_id=cid)

        # L2 — enforce-only suspected-violation escalation (deterministic pre-check, no LLM).
        if mode == MODE_ENFORCE:
            suspicion = self._l2_suspect(event, context)
            if suspicion is not None:
                cid, reason = suspicion
                return Decision.escalate(layer=2, reason=reason, criterion_id=cid)

            # L3 — circuit breaker: once the flow has breached an L3 limit (too many failures /
            # retries), pause further actions for human review. (L3 still does NOT inspect the
            # individual action — it gates on the run's accumulated QA state.)
            l3 = self._l3_breach(context)
            if l3 is not None:
                return Decision.escalate(layer=3, reason=l3, criterion_id="failure_count_trigger")

        return Decision.allow()

    def _l3_breach(self, context: PolicyContext) -> Optional[str]:
        """Enforce-mode L3 circuit breaker: the flow's accumulated failures crossed the Layer-3
        failure_count_trigger, so the next action escalates for review. (L3 gates on the run's QA
        state, not the individual action.) Returns a reason or None."""
        try:
            fail_trigger = int(self.criteria.qa.effective("failure_count_trigger"))
        except (TypeError, ValueError):
            return None
        if fail_trigger and context.failed_steps >= fail_trigger:
            return f"[L3] 누적 실패 {context.failed_steps}회 ≥ 한계선 {fail_trigger} — 회로 차단(검토 필요)"
        return None

    def _l2_suspect(
        self, event: HarnessEvent, context: PolicyContext
    ) -> Optional[tuple[str, str]]:
        """Deterministic Layer-2 structural checks (no LLM). Returns ``(criterion_id, reason)`` when
        suspect, else None. The reason embeds both the matched criterion's *id* and its *wording* so
        the operator can tell which of (possibly dozens of) L2 business rules actually fired.

        1. DC-001 (always on): editing a file with no prior Read of that path in the flow.
        2. Criteria-driven: a domain criterion whose *wording* maps to a built-in structural detector
           (e.g. "회귀 테스트 동반" → test-before-change, "검증 우회 금지" → no-skip-verify) is enforced
           deterministically. Value-level rules with no detector stay Judge-scored (advisory).
        """
        tool = event.tool_name or ""
        path = _edit_path(event)
        if tool in _EDIT_TOOLS and path is not None and path not in context.read_paths:
            return ("DC-001", f"[DC-001] {tool} {path} — 수정 전 해당 파일 Read 기록이 없음 (검토 필요)")

        for dc in self.criteria.domain_criteria:
            desc = dc.description or ""
            for pattern, detector in _L2_DETECTORS:
                if pattern.search(desc):
                    reason = detector(event, context)
                    if reason:
                        label = desc.strip()
                        if len(label) > 80:
                            label = label[:79] + "…"
                        return (dc.id, f"[{dc.id}] {label} — {reason}")
                    break  # this criterion matched a detector but did not fire — move to next criterion
        return None

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
