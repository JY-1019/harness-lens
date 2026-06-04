"""Enforce the 3-Layer criteria on the harness (design §3 — instructions ≈ system prompt).

harness-lens manages the harness as three layers; for Claude Code / Codex to actually *follow*
them, the layers are rendered into the agent-instruction file (CLAUDE.md / AGENTS.md) as a
managed block. That file is the closest AHE-controllable analogue of the agent's system prompt,
so writing the layers there is how the otherwise black-box agent is bound to them.

The block lives between two markers so re-running enforcement (or ``install``) refreshes only the
managed region and never disturbs the user's own instructions. The body is generated from the
live :class:`ThreeLayerCriteria`, so editing ``criteria.yaml`` and re-enforcing keeps the agent's
instructions in sync with what the Judge / QA layers actually evaluate.
"""

from __future__ import annotations

from pathlib import Path

from .criteria import ThreeLayerCriteria
from .detector import Platform

MARKER_START = "<!-- harness-lens:3-layer:start (managed — edits inside this block are overwritten) -->"
MARKER_END = "<!-- harness-lens:3-layer:end -->"


def instruction_target(platform: Platform) -> Path:
    """The agent-instruction file enforcement writes to for ``platform`` (global scope)."""
    return platform.settings_path.parent / platform.instruction_file


def render_block(criteria: ThreeLayerCriteria) -> str:
    """Render the managed 3-Layer directive block (markers included)."""
    invariants = criteria.invariants or ["(정의된 invariant 없음)"]
    inv_lines = "\n".join(f"  - {rule}" for rule in invariants)

    if criteria.domain_criteria:
        dc_lines = "\n".join(
            f"  - [{dc.id}] {dc.description}" for dc in criteria.domain_criteria
        )
    else:
        dc_lines = "  - (정의된 domain criterion 없음)"

    # Render the *effective* thresholds (a guarded one-off override beats the persisted config),
    # so the agent is instructed against the same numbers find_failure_patterns evaluates against.
    qa_lines = "\n".join(
        f"  - {key}: {criteria.qa.effective(key)}" for key in criteria.qa.config.as_dict()
    )

    body = (
        "## harness-lens 3-Layer 하네스 (필수 준수)\n\n"
        "이 작업은 harness-lens 가 3개 Layer 로 관리합니다. 모든 단계에서 아래 Layer 를 따르세요.\n\n"
        "### Layer 1 — Invariants (절대 위반 금지)\n"
        f"{inv_lines}\n\n"
        "### Layer 2 — Domain criteria (행동 기준)\n"
        f"{dc_lines}\n\n"
        "### Layer 3 — QA thresholds (품질 한계선)\n"
        f"{qa_lines}\n\n"
        "Layer 1 은 어떤 경우에도 위반하지 않습니다. Layer 2 기준에 맞게 행동하고, "
        "Layer 3 한계선(재시도/실패/지연/품질)을 넘기지 않도록 작업하세요.\n"
    )
    return f"{MARKER_START}\n{body}{MARKER_END}\n"


def merge_block(existing: str, block: str) -> str:
    """Insert or replace the managed block in ``existing`` instruction-file text.

    A previously-enforced block (between the markers) is replaced in place; otherwise the
    block is appended, preserving the user's existing content either way.
    """
    start = existing.find(MARKER_START)
    if start != -1:
        end = existing.find(MARKER_END, start)
        if end != -1:
            end += len(MARKER_END)
            # Swallow a trailing newline so repeated enforcement doesn't accrete blank lines.
            if end < len(existing) and existing[end] == "\n":
                end += 1
            return existing[:start] + block + existing[end:]
        # Malformed: a start marker with no end marker. Drop the partial block
        # (everything from the marker onward) and re-append a clean one, so
        # enforce refreshes the managed region instead of leaving stale directives.
        existing = existing[:start]
    if not existing.strip():
        return block
    sep = "" if existing.endswith("\n") else "\n"
    return f"{existing}{sep}\n{block}"


def strip_block(text: str) -> str:
    """Remove the managed 3-Layer block (if any) from instruction-file ``text``.

    Used so AHE evolution of the instruction file only ever sees and rewrites the user's own
    content: the non-evolvable Layer 1/2 (and Layer 3) block is stripped before the file goes
    to the evolver and re-applied from ``criteria.yaml`` afterwards, so a proposal can never
    delete or mutate it. A start marker with no end marker is treated as malformed and dropped
    from the marker onward.
    """
    start = text.find(MARKER_START)
    if start == -1:
        return text
    end = text.find(MARKER_END, start)
    if end == -1:
        rest = ""
    else:
        end += len(MARKER_END)
        if end < len(text) and text[end] == "\n":
            end += 1
        rest = text[end:]
    before = text[:start].rstrip("\n")
    rest = rest.lstrip("\n")
    if before and rest:
        return f"{before}\n\n{rest}"
    if before:
        return f"{before}\n"
    return rest


def apply_to_instruction(criteria: ThreeLayerCriteria, platform: Platform, manager) -> tuple[Path, bool]:
    """Write the managed 3-Layer block into ``platform``'s instruction file via ``manager``.

    Returns ``(target_path, changed)``. The apply (and its backup) is skipped when the block is
    already up to date, so re-enforcement on every install does not pile up backups. ``manager``
    is a :class:`~harness_lens.components.ComponentManager`; it rejects a non-editable component.
    """
    target = instruction_target(platform)
    block = render_block(criteria)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    merged = merge_block(existing, block)
    changed = merged != existing
    if changed:
        manager.apply(platform.instruction_file, target, merged)
    return target, changed
