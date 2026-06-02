"""Pillar 2 — Experience Observability.

Compress trajectories into a 3-tier drill-down so an agent can consume only as
much as it needs:

    Tier 1: Flow summaries (success/fail, tokens, time)
    Tier 2: Task-level failure patterns
    Tier 3: Step-level raw evidence (loaded only on drill-down)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .criteria.qa import QACriteria
from .store import Session, Step, StorageBackend


@dataclass
class FlowSummary:
    session_id: str
    platform: str
    status: str
    total_tokens: int
    duration_s: Optional[float]
    task_count: int
    step_count: int
    failure_count: int
    layer2_avg: Optional[float]
    gap_count: int = 0          # steps the platform could not observe (Codex "관측 불가")
    gap_ratio: float = 0.0      # gap_count / step_count


class ExperienceCorpus:
    def __init__(self, store: StorageBackend, qa: Optional[QACriteria] = None):
        self.store = store
        self.qa = qa or QACriteria()

    # -- Tier 1 ---------------------------------------------------------- #
    def tier1_summary(self, limit: int = 20, only_failed: bool = False) -> list[FlowSummary]:
        summaries = []
        for session in self.store.recent_sessions(limit=limit, only_failed=only_failed):
            steps = self.store.steps_for_session(session.session_id)
            summaries.append(self._summarize(session, steps))
        return summaries

    def _summarize(self, session: Session, steps: list[Step]) -> FlowSummary:
        scored = [s.layer2_score for s in steps if s.layer2_score is not None]
        duration = (session.ended_at - session.started_at) if session.ended_at else None
        gap_count = sum(1 for s in steps if s.observed is False)
        return FlowSummary(
            session_id=session.session_id,
            platform=session.platform,
            status=session.status,
            total_tokens=session.total_tokens,
            duration_s=duration,
            task_count=len({s.task_id for s in steps}),
            step_count=len(steps),
            failure_count=sum(1 for s in steps if s.success is False),
            layer2_avg=(sum(scored) / len(scored)) if scored else None,
            gap_count=gap_count,
            gap_ratio=(gap_count / len(steps)) if steps else 0.0,
        )

    # -- Tier 2 ---------------------------------------------------------- #
    def tier2_patterns(self, since: Optional[float] = None) -> list[dict]:
        return self.qa.find_failure_patterns(self.store.all_steps(since=since))

    # -- Tier 3 ---------------------------------------------------------- #
    def tier3_evidence(self, pattern_id: str, since: Optional[float] = None) -> list[Step]:
        steps = self.store.all_steps(since=since)
        return [s for s in steps if f"{s.tool_name}:{s.task_category}" == pattern_id]

    # -- gaps (Codex 관측 불가) ------------------------------------------ #
    def overall_gap_ratio(self, since: Optional[float] = None) -> float:
        steps = self.store.all_steps(since=since)
        if not steps:
            return 0.0
        return sum(1 for s in steps if s.observed is False) / len(steps)

    def pattern_gap_ratio(self, pattern_id: str, since: Optional[float] = None) -> float:
        """Fraction of a pattern's steps that were unobserved.

        The evolver holds proposals for gap-dominated patterns: when too much of the
        evidence is missing, a prediction built on it is not trustworthy.
        """
        evidence = self.tier3_evidence(pattern_id, since=since)
        if not evidence:
            return 0.0
        return sum(1 for s in evidence if s.observed is False) / len(evidence)

    # -- Agent-facing compression --------------------------------------- #
    def to_agent_prompt(self, since: Optional[float] = None, drill_pattern: Optional[str] = None) -> str:
        """Render Tier 1→2 always, Tier 3 only for an explicitly drilled pattern.

        Never dumps the full raw trajectory.
        """
        lines: list[str] = ["## Tier 1 — Flow summaries"]
        for fs in self.tier1_summary():
            dur = f"{fs.duration_s:.0f}s" if fs.duration_s else "?"
            l2 = f"{fs.layer2_avg:.2f}" if fs.layer2_avg is not None else "n/a"
            gap = f" gap={fs.gap_ratio:.0%}" if fs.gap_count else ""
            lines.append(
                f"- {fs.session_id[:8]} [{fs.status}] tokens={fs.total_tokens} {dur} "
                f"tasks={fs.task_count} steps={fs.step_count} fails={fs.failure_count} L2={l2}{gap}"
            )

        lines.append("\n## Tier 2 — Failure patterns")
        patterns = self.tier2_patterns(since=since)
        if not patterns:
            lines.append("- (none crossed Layer-3 thresholds)")
        for p in patterns:
            gap = self.pattern_gap_ratio(p["pattern_id"], since=since)
            gap_note = f" [gap={gap:.0%} — 관측 불가, evidence incomplete]" if gap else ""
            lines.append(f"- {p['pattern_id']}: {', '.join(p['reasons'])} (fails={p['failure_count']}){gap_note}")

        if drill_pattern:
            lines.append(f"\n## Tier 3 — Evidence for {drill_pattern}")
            for s in self.tier3_evidence(drill_pattern, since=since):
                if s.observed is False:
                    # Never fabricate detail for an unobserved step; mark it as a gap so the
                    # Debugger treats the trajectory as incomplete rather than inferring cause.
                    lines.append(f"- {s.step_id[:8]} 관측 불가 (gap — tool not captured by Codex hooks)")
                    continue
                lines.append(
                    f"- {s.step_id[:8]} success={s.success} retry={s.retry_count} "
                    f"in={s.input_summary[:80]!r} out={s.output_summary[:80]!r}"
                )
        return "\n".join(lines)
