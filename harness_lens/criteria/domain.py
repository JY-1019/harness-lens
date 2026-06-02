"""Layer 2 — domain criteria + LLM Judge.

Criteria are human-managed in ``criteria.yaml``. Each is scored by an LLM Judge
that returns a strict JSON verdict. AHE may *propose* changes here but never
applies them automatically (that is the QA layer's job).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..llm import LLMClient, LLMUnavailable

_JUDGE_SYSTEM = (
    "You are a strict evaluator of an AI coding agent's single step. "
    "Answer ONLY with a JSON object of the form "
    '{"pass": true|false, "reason": "<short>"}. No prose outside the JSON.'
)
_JSON_RE = re.compile(r"\{.*\}", re.S)


@dataclass
class DomainCriterion:
    id: str
    description: str
    judge_prompt: str
    weight: float = 1.0

    @classmethod
    def from_dict(cls, data: dict) -> "DomainCriterion":
        return cls(
            id=str(data["id"]),
            description=str(data.get("description", "")),
            judge_prompt=str(data.get("judge_prompt", data.get("description", ""))),
            weight=float(data.get("weight", 1.0)),
        )


@dataclass
class CriterionVerdict:
    criterion_id: str
    passed: bool
    reason: str
    weight: float


class DomainJudge:
    def __init__(self, criteria: list[DomainCriterion], llm: Optional[LLMClient] = None):
        self.criteria = list(criteria)
        self._llm = llm

    @property
    def available(self) -> bool:
        return self._llm is not None and bool(self.criteria)

    def evaluate(self, step, context_steps: Optional[list] = None) -> tuple[Optional[float], list[CriterionVerdict]]:
        """Return ``(weighted_score, verdicts)``.

        ``weighted_score`` is None when the Judge cannot run (no LLM or no
        criteria), which the store records as ``layer2_score = NULL`` (not
        evaluated) rather than a failing score.
        """
        if not self.available:
            return None, []

        context = self._format_context(context_steps or [])
        verdicts: list[CriterionVerdict] = []
        for criterion in self.criteria:
            verdict = self._judge_one(criterion, step, context)
            if verdict is not None:
                verdicts.append(verdict)
        if not verdicts:
            return None, []
        total_weight = sum(v.weight for v in verdicts) or 1.0
        score = sum(v.weight for v in verdicts if v.passed) / total_weight
        return score, verdicts

    def _judge_one(self, criterion: DomainCriterion, step, context: str) -> Optional[CriterionVerdict]:
        prompt = (
            f"{criterion.judge_prompt}\n\n"
            f"Recent context:\n{context}\n\n"
            f"Step under review:\n"
            f"- tool: {step.tool_name}\n- category: {step.task_category}\n"
            f"- input: {step.input_summary}\n- output: {step.output_summary}\n"
            f"- success: {step.success}\n"
        )
        try:
            raw = self._llm.complete(_JUDGE_SYSTEM, prompt, max_tokens=300)
        except LLMUnavailable:
            return None
        parsed = self._parse(raw)
        if parsed is None:
            return None
        passed = self._as_bool(parsed.get("pass"))
        if passed is None:
            # Malformed schema (e.g. "pass": "maybe"); don't guess — bool("false") is True,
            # which would silently inflate the score. Skip this verdict instead.
            return None
        return CriterionVerdict(
            criterion_id=criterion.id,
            passed=passed,
            reason=str(parsed.get("reason", "")),
            weight=criterion.weight,
        )

    @staticmethod
    def _as_bool(value) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "1"):
                return True
            if low in ("false", "no", "0"):
                return False
        return None

    @staticmethod
    def _parse(raw: str) -> Optional[dict]:
        match = _JSON_RE.search(raw or "")
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and "pass" in data else None

    @staticmethod
    def _format_context(context_steps: list) -> str:
        if not context_steps:
            return "(none)"
        lines = [f"- {s.tool_name}[{s.task_category}] success={s.success}" for s in context_steps[-5:]]
        return "\n".join(lines)
