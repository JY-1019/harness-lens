"""Pillar 3 — Evolve agent.

Turns a :class:`~harness_lens.agents.debugger.FailureDiagnosis` into an
:class:`~harness_lens.store.EvolutionCandidate` that *must*:

* target Layer 3 only, and
* target an editable external component, and
* carry a falsifiable prediction (metric + value for the next round).

Anything else is refused.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from ..components import EDITABLE_COMPONENTS
from ..criteria.qa import QACriteria
from ..llm import LLMClient
from ..store import EvolutionCandidate

_SUPPORTED_METRICS = ("failure_rate", "retry_rate", "failure_count")
_SYSTEM = (
    "You propose a single Layer-3 (automated QA) change to fix a diagnosed failure "
    "pattern in an AI coding agent's external scaffolding. Constraints: target_layer "
    "must be 3; the change must touch only the given editable component; you must "
    "commit to a falsifiable prediction. "
    f"predicted_metric must be one of {list(_SUPPORTED_METRICS)} (lower is better); a "
    "*_rate value must be a fraction in 0..1, failure_count a non-negative integer. "
    "proposed_change shape depends on the component: for qa.py it MUST be "
    '{"params": {<layer3 key>: <number>}} (keys: retry_threshold, latency_multiplier, '
    "failure_count_trigger, quality_threshold); for any other component it MUST be "
    '{"path": "<relative path>", "content": "<full new file content>"}. '
    'Answer ONLY with JSON: {"proposed_change": {..}, "prediction": "<text>", '
    '"predicted_metric": "<metric>", "predicted_value": <number>, "target_layer": 3}'
)
_JSON_RE = re.compile(r"\{.*\}", re.S)


class ProposalError(RuntimeError):
    """Raised when a diagnosis cannot yield a valid Layer-3/external proposal."""


class EvolveAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def propose(self, diagnosis, current_content: Optional[str] = None) -> EvolutionCandidate:
        if diagnosis.needs_human_review or not diagnosis.affected_component:
            raise ProposalError(
                f"{diagnosis.failure_pattern}: cause needs human review (black-box internal); "
                "no automated proposal"
            )
        if diagnosis.affected_component not in EDITABLE_COMPONENTS:
            raise ProposalError(f"{diagnosis.affected_component!r} is not editable; refusing proposal")

        prompt = (
            f"Failure pattern: {diagnosis.failure_pattern}\n"
            f"Diagnosis: {diagnosis.diagnosis}\n"
            f"Editable component to change: {diagnosis.affected_component}\n"
        )
        if current_content is not None:
            # File components are overwritten wholesale with the returned `content`, so the
            # model must see the existing file or it will drop unrelated instructions it
            # cannot guess. Give it the current content to edit rather than rewrite blind.
            prompt += (
                "Current content of this file (your `content` REPLACES it wholesale, so "
                "preserve everything still needed and add/adjust only what the fix requires):\n"
                f"---\n{current_content}\n---\n"
            )
        prompt += "Propose the Layer-3 change and your prediction."
        # File components must return the full replacement file in `content`; 500 tokens
        # truncates any non-trivial CLAUDE.md/settings into invalid JSON. qa.py only returns
        # a small params object, so it keeps the tight budget.
        max_tokens = 500 if diagnosis.affected_component == "qa.py" else 4000
        data = self._parse(self.llm.complete(_SYSTEM, prompt, max_tokens=max_tokens))

        try:
            target_layer = int(data.get("target_layer", 3))
        except (TypeError, ValueError):
            # A malformed field (null, "Layer 3", ...) must be held like any other invalid
            # proposal, not crash the whole evolve run (the service only catches ProposalError).
            raise ProposalError("target_layer is not an integer; rejected")
        if target_layer != 3:
            raise ProposalError("proposal did not target Layer 3; rejected")
        metric = str(data.get("predicted_metric", "")).strip()
        if metric not in _SUPPORTED_METRICS:
            # All supported metrics are lower-is-better; silently coercing an unsupported
            # metric (e.g. success_rate) would verify the prediction against the wrong
            # condition. Reject so the prediction stays falsifiable and meaningful.
            raise ProposalError(
                f"predicted_metric {metric!r} is not one of {list(_SUPPORTED_METRICS)}; rejected"
            )
        predicted_value = self._as_float(data.get("predicted_value"))
        if predicted_value is None:
            # No numeric target → the prediction is not falsifiable; refuse (design §15/§11).
            raise ProposalError(
                f"{diagnosis.failure_pattern}: proposal lacks a numeric predicted_value; "
                "a falsifiable prediction is required"
            )
        # The verifier treats *_rate metrics as 0..1 fractions; a percentage like 50 would
        # pass `actual <= predicted + tolerance` trivially and confirm regardless of reality.
        if metric in ("failure_rate", "retry_rate") and not 0.0 <= predicted_value <= 1.0:
            raise ProposalError(
                f"{diagnosis.failure_pattern}: {metric} prediction {predicted_value} is out of "
                "the 0..1 range; rejected"
            )
        if metric == "failure_count" and predicted_value < 0:
            raise ProposalError(
                f"{diagnosis.failure_pattern}: failure_count prediction cannot be negative; rejected"
            )

        proposed_change = data.get("proposed_change")
        if not isinstance(proposed_change, dict):
            proposed_change = {}
        # Reject here if the payload is not one apply_evolution can act on, rather than
        # persisting a candidate that only fails (ComponentError) at apply time. qa.py
        # needs `params`; file components need `path`+`content`.
        if diagnosis.affected_component == "qa.py":
            params = proposed_change.get("params")
            if not isinstance(params, dict) or not params:
                raise ProposalError(
                    f"{diagnosis.failure_pattern}: qa.py proposal lacks a non-empty 'params' object"
                )
            # Require at least one *recognized* Layer-3 key. A params object of only
            # unknown keys (e.g. {"foo": 1}) would pass the non-empty check yet
            # _apply_layer3_params drops every key, leaving an applied candidate that is
            # a silent no-op. Reject it here so the prediction stays meaningful.
            if not (set(params) & QACriteria.EVOLVABLE_KEYS):
                raise ProposalError(
                    f"{diagnosis.failure_pattern}: qa.py 'params' has no recognized Layer-3 "
                    f"key (expected one of {sorted(QACriteria.EVOLVABLE_KEYS)})"
                )
        elif not (proposed_change.get("path") and proposed_change.get("content")):
            raise ProposalError(
                f"{diagnosis.failure_pattern}: proposal lacks 'path'+'content' for "
                f"{diagnosis.affected_component}"
            )

        return EvolutionCandidate(
            failure_pattern=diagnosis.failure_pattern,
            diagnosis=diagnosis.diagnosis,
            target_component=diagnosis.affected_component,
            affected_step_ids=list(diagnosis.affected_step_ids),
            proposed_change=proposed_change,
            target_layer=3,
            prediction=str(data.get("prediction", "")),
            predicted_metric=metric,
            predicted_value=predicted_value,
            status="proposed",
        )

    @staticmethod
    def _parse(raw: str) -> dict:
        match = _JSON_RE.search(raw or "")
        if not match:
            raise ProposalError("LLM did not return a JSON proposal")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ProposalError(f"proposal JSON was invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise ProposalError("proposal JSON was not an object")
        return data

    @staticmethod
    def _as_float(value) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
