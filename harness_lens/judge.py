"""Judge drift detection.

The Layer-2 Judge is sampled (default 20%). To keep trusting it, sampled scores
are periodically labelled by a human (``harness-lens review``); this module
computes the agreement rate and recommends recalibration when it drifts below a
threshold (default 85%).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .store import JudgeSample, StorageBackend

AGREEMENT_THRESHOLD = 0.85
# A Judge score and a human label agree when both fall on the same side of 0.5.
_PASS_CUTOFF = 0.5


@dataclass
class JudgeStatus:
    reviewed: int
    agreed: int
    agreement_rate: Optional[float]
    drift: bool
    threshold: float = AGREEMENT_THRESHOLD

    @property
    def recommendation(self) -> str:
        if self.agreement_rate is None:
            return "리뷰된 샘플이 없습니다. `harness-lens review` 로 라벨링하세요."
        if self.drift:
            return f"일치율 {self.agreement_rate:.0%} < {self.threshold:.0%}: criteria 재조정을 권고합니다."
        return f"일치율 {self.agreement_rate:.0%}: Judge 신뢰 가능."


class JudgeMonitor:
    def __init__(self, store: StorageBackend, threshold: float = AGREEMENT_THRESHOLD):
        self.store = store
        self.threshold = threshold

    def record_label(self, sample: JudgeSample, human_label: float) -> JudgeSample:
        import time

        sample.human_label = human_label
        sample.agreement = (sample.judge_score >= _PASS_CUTOFF) == (human_label >= _PASS_CUTOFF)
        sample.reviewed_at = time.time()
        self.store.update_judge_sample(sample)
        return sample

    def pending_samples(self) -> list[JudgeSample]:
        return [s for s in self.store.judge_samples() if s.reviewed_at is None]

    def status(self) -> JudgeStatus:
        reviewed = self.store.judge_samples(reviewed_only=True)
        agreed = sum(1 for s in reviewed if s.agreement)
        rate = (agreed / len(reviewed)) if reviewed else None
        drift = rate is not None and rate < self.threshold
        return JudgeStatus(
            reviewed=len(reviewed), agreed=agreed, agreement_rate=rate, drift=drift,
            threshold=self.threshold,
        )
