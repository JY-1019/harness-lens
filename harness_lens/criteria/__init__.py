"""Three-layer criteria system.

* Layer 1 — :mod:`.invariant`: absolute, deterministic, never AHE-editable.
* Layer 2 — :mod:`.domain`: human-managed criteria evaluated by an LLM Judge.
* Layer 3 — :mod:`.qa`: automated thresholds; the only AHE-evolvable layer.

:mod:`.layer` ties them together and adds :class:`.layer.CriteriaGuard`.
"""

from .domain import DomainCriterion, DomainJudge
from .engine import CriteriaEngine
from .invariant import InvariantChecker, InvariantViolation
from .layer import CriteriaGuard, CriteriaViolation, ThreeLayerCriteria, DEFAULT_CRITERIA_YAML
from .qa import QAConfig, QACriteria, layer3_in_range

__all__ = [
    "InvariantChecker", "InvariantViolation",
    "DomainCriterion", "DomainJudge",
    "QAConfig", "QACriteria", "layer3_in_range",
    "ThreeLayerCriteria", "CriteriaGuard", "CriteriaViolation",
    "CriteriaEngine", "DEFAULT_CRITERIA_YAML",
]
