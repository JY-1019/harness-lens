"""Pillar 3 — Decision Observability (falsifiable contracts).

Every applied evolution carries a prediction. The next round we measure the
actual metric and compare:

* hit  → candidate confirmed.
* miss → candidate rolled back (the caller restores the component backup).

Supported metrics (all "lower is better", measured per failure pattern):
``failure_rate``, ``retry_rate``, ``failure_count``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .store import DecisionRecord, EvolutionCandidate, Step, StorageBackend

# Absolute tolerance on a predicted rate/count before a prediction counts as a miss.
DEFAULT_TOLERANCE = 0.1


@dataclass
class VerifyResult:
    candidate_id: str
    predicted_metric: str
    predicted_value: Optional[float]
    actual_value: Optional[float]
    was_correct: Optional[bool]
    note: str = ""


class DecisionVerifier:
    def __init__(self, store: StorageBackend, tolerance: float = DEFAULT_TOLERANCE):
        self.store = store
        self.tolerance = tolerance

    def record_prediction(self, candidate: EvolutionCandidate) -> DecisionRecord:
        record = DecisionRecord(
            candidate_id=candidate.candidate_id,
            prediction=candidate.prediction,
            predicted_value=candidate.predicted_value,
        )
        return self.store.add_decision(record)

    def verify_after_round(self, candidate_id: str) -> VerifyResult:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            return VerifyResult(candidate_id, "", None, None, None, note="candidate not found")
        if candidate.applied_at is None:
            return VerifyResult(
                candidate_id, candidate.predicted_metric, candidate.predicted_value,
                None, None, note="candidate not applied yet",
            )

        steps = [s for s in self.store.all_steps(since=candidate.applied_at)
                 if self._matches_pattern(s, candidate.failure_pattern)]
        actual = self._metric(candidate.predicted_metric, steps)
        if actual is None:
            # No matching post-apply steps yet: keep the prediction *pending*. Stamping a
            # verified_at here would both resolve the candidate prematurely and make the
            # next verify_after_round add a second decision row (the original pending row
            # would no longer be found), polluting prediction history.
            return VerifyResult(
                candidate_id, candidate.predicted_metric, candidate.predicted_value,
                None, None, note="no evidence yet",
            )
        was_correct = self._judge(candidate.predicted_value, actual)

        self._record_outcome(candidate_id, actual, was_correct)
        if was_correct is True:
            candidate.status = "confirmed"
            self.store.update_candidate(candidate)
        # On a miss, the status stays "applied" here. The caller (LensService) then runs
        # the actual file restore and only marks the candidate "rolled_back" once that
        # succeeds — so a missing/corrupt backup (which makes the restore raise) never
        # leaves the ledger claiming a rollback that never touched the live file.

        return VerifyResult(
            candidate_id, candidate.predicted_metric, candidate.predicted_value,
            actual, was_correct, note="",
        )

    # -- internals ------------------------------------------------------- #
    @staticmethod
    def _matches_pattern(step: Step, pattern: str) -> bool:
        return f"{step.tool_name}:{step.task_category}" == pattern

    @staticmethod
    def _metric(name: str, steps: list[Step]) -> Optional[float]:
        if not steps:
            return None
        completed = [s for s in steps if s.success is not None]
        if not completed:
            return None
        if name == "failure_rate":
            return sum(1 for s in completed if s.success is False) / len(completed)
        if name == "retry_rate":
            return sum(1 for s in completed if s.retry_count) / len(completed)
        if name == "failure_count":
            return float(sum(1 for s in completed if s.success is False))
        return None

    def _judge(self, predicted: Optional[float], actual: Optional[float]) -> Optional[bool]:
        if predicted is None or actual is None:
            return None
        return actual <= predicted + self.tolerance

    def _record_outcome(self, candidate_id: str, actual: Optional[float], was_correct: Optional[bool]) -> None:
        pending = [d for d in self.store.decisions()
                   if d.candidate_id == candidate_id and d.verified_at is None]
        record = pending[-1] if pending else None
        if record is None:
            candidate = self.store.get_candidate(candidate_id)
            record = self.store.add_decision(DecisionRecord(
                candidate_id=candidate_id,
                prediction=candidate.prediction if candidate else "",
                predicted_value=candidate.predicted_value if candidate else None,
            ))
        record.actual_value = actual
        record.was_correct = was_correct
        record.verified_at = time.time()
        self.store.update_decision(record)

    def hit_rate(self) -> Optional[float]:
        verified = [d for d in self.store.decisions() if d.was_correct is not None]
        if not verified:
            return None
        return sum(1 for d in verified if d.was_correct) / len(verified)
