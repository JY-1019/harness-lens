"""CriteriaEngine — evaluate a completed step through the layers.

Layer 1 runs on every step (deterministic). Layer 2 runs on a sample (default
20%) when a Judge is available; otherwise ``layer2_score`` stays ``None`` (not
evaluated). A sampled Judge score is also stored as a :class:`JudgeSample` so the
drift monitor can later compare it against a human label.
"""

from __future__ import annotations

import json
import random
from typing import Optional

from ..llm import LLMClient
from ..store import JudgeSample, StorageBackend
from .invariant import InvariantViolation
from .layer import ThreeLayerCriteria


class CriteriaEngine:
    def __init__(
        self,
        criteria: ThreeLayerCriteria,
        store: StorageBackend,
        llm: Optional[LLMClient] = None,
        sample_rate: float = 0.2,
        rng: Optional[random.Random] = None,
    ):
        self.criteria = criteria
        self.store = store
        self.invariant = criteria.invariant_checker()
        self.judge = criteria.domain_judge(llm)
        self.sample_rate = sample_rate
        self.rng = rng or random.Random()
        self.last_violations: list[InvariantViolation] = []

    def evaluate(self, step, context_steps: Optional[list] = None):
        passed, violations = self.invariant.check(step)
        step.layer1_passed = passed
        self.last_violations = violations
        # Persist *which* invariants were checked and which fired (natural-language attribution),
        # so the monitor can show the harness applied to this step — not just a pass/fail bit.
        step.layer1_detail = json.dumps({
            "checked": list(self.criteria.invariants),
            "violations": [{"rule": v.rule, "detail": v.detail} for v in violations],
        }, ensure_ascii=False)

        if self.judge.available and self.rng.random() < self.sample_rate:
            score, verdicts = self.judge.evaluate(step, context_steps)
            step.layer2_score = score
            # Keep the per-criterion verdicts (id, pass, reason) the Judge produced rather than
            # discarding them — this is the natural-language "why" behind the Layer-2 score.
            step.layer2_verdicts = json.dumps([
                {"criterion_id": v.criterion_id, "passed": v.passed, "reason": v.reason, "weight": v.weight}
                for v in verdicts
            ], ensure_ascii=False)
            if score is not None:
                self.store.add_judge_sample(JudgeSample(step_id=step.step_id, judge_score=score))
        return step
