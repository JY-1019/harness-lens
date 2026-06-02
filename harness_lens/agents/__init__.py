"""LLM agents.

* :class:`.debugger.DebuggerAgent` — Pillar 2 failure diagnosis.
* :class:`.evolver.EvolveAgent` — Pillar 3 evolution proposals with predictions.
"""

from .debugger import DebuggerAgent, FailureDiagnosis
from .evolver import EvolveAgent, ProposalError

__all__ = ["DebuggerAgent", "FailureDiagnosis", "EvolveAgent", "ProposalError"]
