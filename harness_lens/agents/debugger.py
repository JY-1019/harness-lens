"""Pillar 2 — Debugger agent.

Consumes the tiered :class:`~harness_lens.experience.ExperienceCorpus` and asks
the LLM to diagnose each failure pattern. A diagnosis may only point at an
*editable external component*; if the root cause is the agent's black-box
internals, it is flagged ``needs_human_review`` instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..components import EDITABLE_COMPONENTS
from ..llm import LLMClient

_SYSTEM = (
    "You diagnose failure patterns in an AI coding agent's trajectory. "
    "You may only attribute a fixable cause to one of these EDITABLE external components: "
    f"{sorted(EDITABLE_COMPONENTS)}. "
    "If the real cause is the agent's internal prompt/tools/middleware (black box), "
    'set affected_component to "" and needs_human_review to true. '
    "Some evidence may be marked '관측 불가' (gap) — these are steps the platform could not "
    "observe. Treat such trajectories as INCOMPLETE: do not infer a cause from unobserved "
    "gaps. If a pattern's evidence is mostly gaps, set needs_human_review to true. "
    'Answer ONLY with JSON: '
    '{"diagnosis": "<short>", "affected_component": "<one of the list or empty>", '
    '"needs_human_review": true|false}'
)
_JSON_RE = re.compile(r"\{.*\}", re.S)
_MAX_PATTERNS = 8


@dataclass
class FailureDiagnosis:
    failure_pattern: str
    affected_step_ids: list[str]
    diagnosis: str
    affected_component: str = ""
    needs_human_review: bool = False


class DebuggerAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def diagnose(self, corpus) -> list[FailureDiagnosis]:
        patterns = corpus.tier2_patterns()[:_MAX_PATTERNS]
        diagnoses: list[FailureDiagnosis] = []
        for pattern in patterns:
            prompt = (
                corpus.to_agent_prompt(drill_pattern=pattern["pattern_id"])
                + f"\n\nDiagnose pattern {pattern['pattern_id']} ({', '.join(pattern['reasons'])})."
            )
            raw = self.llm.complete(_SYSTEM, prompt, max_tokens=400)
            diagnoses.append(self._to_diagnosis(pattern, raw))
        return diagnoses

    def _to_diagnosis(self, pattern: dict, raw: str) -> FailureDiagnosis:
        data = self._parse(raw)
        component = str(data.get("affected_component", "")).strip()
        # Parse like the domain judge: a JSON string "false" must read as False. Plain
        # bool("false") is True, which would wrongly hold a valid, editable diagnosis as a
        # black-box / human-review case. Missing or unparseable → default False.
        needs_review = self._as_bool(data.get("needs_human_review"))
        if needs_review is None:
            needs_review = False
        # An uneditable / black-box attribution is downgraded to human review.
        if component not in EDITABLE_COMPONENTS:
            component = ""
            needs_review = True
        return FailureDiagnosis(
            failure_pattern=pattern["pattern_id"],
            affected_step_ids=pattern.get("affected_step_ids", []),
            diagnosis=str(data.get("diagnosis", "")) or ", ".join(pattern["reasons"]),
            affected_component=component,
            needs_human_review=needs_review,
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
    def _parse(raw: str) -> dict:
        match = _JSON_RE.search(raw or "")
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
