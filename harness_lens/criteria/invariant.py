"""Layer 1 — invariants.

Absolute rules that AHE may never modify. Checks are deterministic (no LLM): a
violation is recorded and surfaced as a warning, but hooks never *block* (they
only observe — see :mod:`harness_lens.hooks.record`).

Rules are authored as natural-language strings in ``criteria.yaml``. Each rule is
matched to a built-in detector by keyword so the YAML stays human-readable while
the enforcement stays deterministic. A rule with no matching detector is treated
as advisory (it cannot fail a step on its own).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class InvariantViolation:
    rule: str
    detail: str


# A detector inspects the combined step text and returns a reason string when the
# invariant is violated, or None otherwise.
Detector = Callable[[str], "str | None"]

_PII_HINT = re.compile(r"(주민등록번호|ssn|social security|passport|신용카드|card number)", re.I)
_EXTERNAL_SEND = re.compile(r"(https?://|curl|requests\.post|fetch\(|webfetch|api\.)", re.I)
_PROD_DELETE = re.compile(r"delete\s+from", re.I)
_PROD_MARK = re.compile(r"(prod|production|운영|프로덕션)", re.I)


def _pii_external(text: str) -> "str | None":
    if _PII_HINT.search(text) and _EXTERNAL_SEND.search(text):
        return "personal data appears alongside an external request"
    return None


def _prod_delete(text: str) -> "str | None":
    if _PROD_DELETE.search(text) and _PROD_MARK.search(text):
        return "DELETE issued against what looks like a production database"
    return None


# Keyword → detector. The first keyword found in a rule string selects its detector.
_RULE_DETECTORS: tuple[tuple[re.Pattern[str], Detector], ...] = (
    (re.compile(r"(개인정보|pii|personal)", re.I), _pii_external),
    (re.compile(r"(delete|삭제)", re.I), _prod_delete),
)


class InvariantChecker:
    def __init__(self, rules: list[str]):
        self.rules = list(rules)

    def _detector_for(self, rule: str) -> "Detector | None":
        for pattern, detector in _RULE_DETECTORS:
            if pattern.search(rule):
                return detector
        return None

    def check(self, step) -> tuple[bool, list[InvariantViolation]]:
        """Return ``(passed, violations)`` for a completed step."""
        text = f"{step.tool_name}\n{step.input_summary}\n{step.output_summary}"
        violations: list[InvariantViolation] = []
        for rule in self.rules:
            detector = self._detector_for(rule)
            if detector is None:
                continue
            reason = detector(text)
            if reason:
                violations.append(InvariantViolation(rule=rule, detail=reason))
        return (len(violations) == 0, violations)
