"""Layer 3 — automated QA thresholds.

This is the only AHE-evolvable layer. Its parameters live in ``criteria.yaml``
under ``layer3`` and may be tuned by:

* the user, via a guarded one-off override (:meth:`QACriteria.set_override`), or
* an accepted evolution candidate (:mod:`harness_lens.agents.evolver`).

It also turns raw steps into *failure patterns* that the Debugger agent consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Optional


def layer3_in_range(key: str, value) -> bool:
    """Whether a Layer-3 parameter value is within its sane range.

    Shared by automated evolution (:class:`~harness_lens.service.LensService`) and
    one-off user overrides (:class:`~harness_lens.criteria.layer.CriteriaGuard`) so
    both reject the same out-of-range values (e.g. retry_threshold 0,
    quality_threshold 2) that would otherwise make find_failure_patterns flag
    every group.
    """
    if key in ("retry_threshold", "failure_count_trigger"):
        return value >= 1
    if key == "latency_multiplier":
        return value > 0
    if key == "quality_threshold":
        return 0.0 <= value <= 1.0
    return True


@dataclass
class QAConfig:
    retry_threshold: int = 3
    latency_multiplier: float = 3.0
    failure_count_trigger: int = 3
    quality_threshold: float = 0.85

    @classmethod
    def from_dict(cls, data: dict) -> "QAConfig":
        # Values may arrive from YAML or LLM-proposed params, where a threshold can be
        # quoted ("2") or otherwise non-numeric. Coerce to the field's numeric type so
        # later comparisons in find_failure_patterns never compare numbers to strings;
        # values that cannot be coerced fall back to the default rather than crashing.
        defaults = cls()
        known = {}
        for f in cls.__dataclass_fields__:
            if f not in data:
                continue
            caster = type(getattr(defaults, f))
            try:
                known[f] = caster(data[f])
            except (TypeError, ValueError):
                pass
        return cls(**known)

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


class QACriteria:
    EVOLVABLE_KEYS = frozenset(QAConfig.__dataclass_fields__)

    def __init__(self, config: Optional[QAConfig] = None):
        self.config = config or QAConfig()
        self._overrides: dict[str, float] = {}

    # -- guarded override (user prompt may adjust Layer 3 only) ---------- #
    def set_override(self, key: str, value: float) -> None:
        if key not in self.EVOLVABLE_KEYS:
            raise KeyError(f"{key!r} is not a Layer-3 parameter")
        self._overrides[key] = value

    def effective(self, key: str) -> float:
        if key in self._overrides:
            return self._overrides[key]
        return getattr(self.config, key)

    # -- pattern detection ---------------------------------------------- #
    def find_failure_patterns(self, steps: list) -> list[dict]:
        """Group failing/expensive steps into evolvable patterns.

        Returns one dict per ``(tool_name, task_category)`` group that crosses a
        Layer-3 threshold, each carrying the offending step ids and the metric
        that tripped.
        """
        baseline = self._latency_baseline(steps)
        retry_threshold = self.effective("retry_threshold")
        latency_cap = baseline * self.effective("latency_multiplier") if baseline else None
        failure_trigger = self.effective("failure_count_trigger")
        quality_threshold = self.effective("quality_threshold")

        groups: dict[tuple[str, str], dict] = {}
        for step in steps:
            key = (step.tool_name, step.task_category)
            g = groups.setdefault(key, {"steps": [], "failures": 0, "max_retry": 0, "slow": 0, "low_quality": 0, "slow_ids": set()})
            g["steps"].append(step)
            if step.success is False:
                g["failures"] += 1
            g["max_retry"] = max(g["max_retry"], step.retry_count)
            if latency_cap and step.latency_ms and step.latency_ms > latency_cap:
                g["slow"] += 1
                g["slow_ids"].add(step.step_id)
            if step.layer2_score is not None and step.layer2_score < quality_threshold:
                g["low_quality"] += 1

        patterns: list[dict] = []
        for (tool, category), g in groups.items():
            reasons = []
            if g["failures"] >= failure_trigger:
                reasons.append(f"{g['failures']} failures")
            if g["max_retry"] >= retry_threshold:
                reasons.append(f"retry x{g['max_retry']}")
            if g["slow"]:
                reasons.append(f"{g['slow']} slow steps (> {self.effective('latency_multiplier')}x median)")
            if g["low_quality"]:
                reasons.append(f"{g['low_quality']} low-quality steps (< {quality_threshold} Judge score)")
            if not reasons:
                continue
            patterns.append({
                "pattern_id": f"{tool}:{category}",
                "tool_name": tool,
                "task_category": category,
                "reasons": reasons,
                "affected_step_ids": [
                    s.step_id for s in g["steps"]
                    if s.success is False or s.retry_count
                    or s.step_id in g["slow_ids"]
                    or (s.layer2_score is not None and s.layer2_score < quality_threshold)
                ],
                "failure_count": g["failures"],
                "max_retry": g["max_retry"],
            })
        return patterns

    @staticmethod
    def _latency_baseline(steps: list) -> Optional[float]:
        latencies = [s.latency_ms for s in steps if s.latency_ms]
        return median(latencies) if latencies else None
