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
        )

    # -- Tier 2 ---------------------------------------------------------- #
    def tier2_patterns(self, since: Optional[float] = None) -> list[dict]:
        return self.qa.find_failure_patterns(self.store.all_steps(since=since))

    # -- Tier 3 ---------------------------------------------------------- #
    def tier3_evidence(self, pattern_id: str, since: Optional[float] = None) -> list[Step]:
        steps = self.store.all_steps(since=since)
        return [s for s in steps if f"{s.tool_name}:{s.task_category}" == pattern_id]

    # -- Agent-facing compression --------------------------------------- #
    def to_agent_prompt(self, since: Optional[float] = None, drill_pattern: Optional[str] = None) -> str:
        """Render Tier 1→2 always, Tier 3 only for an explicitly drilled pattern.

        Never dumps the full raw trajectory.
        """
        lines: list[str] = ["## Tier 1 — Flow summaries"]
        for fs in self.tier1_summary():
            dur = f"{fs.duration_s:.0f}s" if fs.duration_s else "?"
            l2 = f"{fs.layer2_avg:.2f}" if fs.layer2_avg is not None else "n/a"
            lines.append(
                f"- {fs.session_id[:8]} [{fs.status}] tokens={fs.total_tokens} {dur} "
                f"tasks={fs.task_count} steps={fs.step_count} fails={fs.failure_count} L2={l2}"
            )

        lines.append("\n## Tier 2 — Failure patterns")
        patterns = self.tier2_patterns(since=since)
        if not patterns:
            lines.append("- (none crossed Layer-3 thresholds)")
        for p in patterns:
            lines.append(f"- {p['pattern_id']}: {', '.join(p['reasons'])} (fails={p['failure_count']})")

        if drill_pattern:
            lines.append(f"\n## Tier 3 — Evidence for {drill_pattern}")
            for s in self.tier3_evidence(drill_pattern, since=since):
                lines.append(
                    f"- {s.step_id[:8]} success={s.success} retry={s.retry_count} "
                    f"in={s.input_summary[:80]!r} out={s.output_summary[:80]!r}"
                )
        return "\n".join(lines)
