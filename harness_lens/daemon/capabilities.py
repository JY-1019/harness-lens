"""Per-source capability matrix + the :class:`Decision` the policy engine returns.

Claude Code and Codex do not expose the same control surface. The policy engine is
written once against the richest surface (Claude Code) and produces a
:class:`Decision`; :func:`downgrade` then clamps that decision to what the *source*
harness can actually express, returning a possibly-changed decision plus a note that
the caller records to the ledger (design: "adapter downgrades + ledger records it").
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

# Capability matrix — exactly the design's table. Keep this the single source of truth;
# adapters and the policy renderer consult it rather than hard-coding per-source rules.
CAPABILITIES: dict[str, dict[str, bool]] = {
    "claude_code": {
        "deny": True,
        "allow": True,
        "escalate": True,
        "update_tool_input": True,
        "inject_context": True,
        "force_continue_on_stop": True,
    },
    "codex": {
        # Codex CLI ≥ 0.139 shares Claude's hook-output *schema*, but its RUNTIME validator only
        # honours PreToolUse permissionDecision "deny" (with a non-empty reason). It rejects
        # permissionDecision "allow"/"ask" and updatedInput ("unsupported permissionDecision:allow").
        # So: deny works; allow = empty output; there is no native ask (escalate collapses to deny);
        # updatedInput is unusable. additionalContext + Stop "block" are fine.
        "deny": True,
        "allow": True,
        "escalate": False,  # no native "ask" at runtime; GUI approval still parks it, then deny/allow
        "update_tool_input": False,  # updatedInput requires the unsupported permissionDecision:allow
        "inject_context": True,
        "force_continue_on_stop": True,
    },
}

# Decision actions.
ALLOW = "allow"
DENY = "deny"
ESCALATE = "escalate"


@dataclass
class Decision:
    """The outcome of evaluating a control event.

    ``action`` is one of allow/deny/escalate. ``layer`` records which layer decided
    (1/2/3 or None for a default allow). ``updated_input`` and ``inject_context`` are
    optional side-effects the harness may apply; they are dropped if the source cannot
    express them. ``downgrades`` accumulates human-readable notes when capability
    clamping changed the decision, so the daemon can write them to the ledger.
    """

    action: str = ALLOW
    layer: Optional[int] = None
    reason: str = ""
    # The specific rule that decided: an L2 domain-criterion id ("DC-017"), the L1 invariant text,
    # or an L3 threshold key ("failure_count_trigger"). Lets the GUI pinpoint *which* rule fired
    # among dozens, rather than making the user parse it out of the reason string.
    criterion_id: Optional[str] = None
    updated_input: Optional[dict] = None
    inject_context: Optional[str] = None
    # Set when this decision will park in the approval queue (escalate). Carried so the
    # adapter/daemon can correlate the eventual resolution back to the originating step.
    needs_approval: bool = False
    downgrades: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls, layer: Optional[int] = None, reason: str = "", **kw) -> "Decision":
        return cls(action=ALLOW, layer=layer, reason=reason, **kw)

    @classmethod
    def deny(cls, layer: int, reason: str, **kw) -> "Decision":
        return cls(action=DENY, layer=layer, reason=reason, **kw)

    @classmethod
    def escalate(cls, layer: int, reason: str, **kw) -> "Decision":
        return cls(action=ESCALATE, layer=layer, reason=reason, needs_approval=True, **kw)


def supports(source: str, capability: str) -> bool:
    return CAPABILITIES.get(source, {}).get(capability, False)


def downgrade(decision: Decision, source: str) -> Decision:
    """Clamp ``decision`` to what ``source`` can express; record what changed.

    The only field that genuinely cannot be downgraded mid-flight is ``escalate``: the
    approval queue still parks it (the GUI/terminal can resolve it for either harness),
    so escalate is left intact here and only its *terminal* rendering differs — see
    :mod:`harness_lens.daemon.adapters`. What we clamp here are the optional side-effects
    (``update_tool_input`` for Codex) so the daemon never returns a field Codex rejects.
    """
    caps = CAPABILITIES.get(source, {})
    notes = list(decision.downgrades)
    updated_input = decision.updated_input
    inject_context = decision.inject_context

    if updated_input is not None and not caps.get("update_tool_input", False):
        notes.append(
            f"updated_input dropped: {source} cannot rewrite tool input (no updatedMCPToolOutput)"
        )
        updated_input = None
    if inject_context is not None and not caps.get("inject_context", False):
        notes.append(f"inject_context dropped: {source} cannot inject additional context")
        inject_context = None

    if notes == decision.downgrades and updated_input is decision.updated_input \
            and inject_context is decision.inject_context:
        return decision
    return replace(
        decision, updated_input=updated_input, inject_context=inject_context, downgrades=notes
    )
