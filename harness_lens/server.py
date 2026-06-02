"""FastMCP server exposing harness-lens to the agent.

Tools (design §12): record_step, get_flow_summary, run_diagnosis,
propose_evolution, apply_evolution, verify_predictions, set_layer3_override,
get_judge_status.

Resources: ledger://recent, ledger://criteria, ledger://predictions,
ledger://judge-status.

The ``mcp`` package is imported lazily so the rest of harness-lens stays
importable without it.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from .service import LensService


def build_server(service: Optional[LensService] = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("MCP server requires the 'mcp' extra (pip install harness-lens[mcp])") from exc

    service = service or LensService()
    mcp = FastMCP("harness-lens")

    @mcp.tool()
    def record_step(
        session_id: str,
        tool_name: str,
        input_summary: str = "",
        output_summary: str = "",
        success: bool = True,
        latency_ms: Optional[int] = None,
    ) -> dict:
        """Record one Step (Layer 1 always; Layer 2 sampled when a Judge is wired)."""
        return asdict(service.record_step(session_id, tool_name, input_summary, output_summary, success, latency_ms))

    @mcp.tool()
    def get_flow_summary(session_id: Optional[str] = None, limit: int = 20, only_failed: bool = False) -> list:
        """Flow / Task / Step tree with Layer-2 scores."""
        return service.get_flow_summary(session_id, limit=limit, only_failed=only_failed)

    @mcp.tool()
    def run_diagnosis() -> list:
        """Pillar 2 — diagnose failure patterns (Debugger agent)."""
        return service.run_diagnosis()

    @mcp.tool()
    def propose_evolution() -> list:
        """Pillar 3 — propose Layer-3 changes with predictions (Evolve agent)."""
        return service.propose_evolution()

    @mcp.tool()
    def apply_evolution(candidate_id: str, confirmed: bool = False) -> dict:
        """Apply a candidate (Layer 3 + external component + confirmed only)."""
        return service.apply_evolution(candidate_id, confirmed)

    @mcp.tool()
    def verify_predictions() -> list:
        """Verify predictions against the next round; confirm or roll back."""
        return [asdict(r) for r in service.verify_predictions()]

    @mcp.tool()
    def set_layer3_override(key: str, value: float, reason: str) -> dict:
        """Guarded one-off Layer-3 override (CriteriaGuard enforced)."""
        return service.set_layer3_override(key, value, reason)

    @mcp.tool()
    def get_judge_status() -> dict:
        """Judge agreement rate + drift recommendation."""
        status = service.get_judge_status()
        return {**asdict(status), "recommendation": status.recommendation}

    @mcp.resource("ledger://recent")
    def recent() -> list:
        return service.get_flow_summary()

    @mcp.resource("ledger://criteria")
    def criteria() -> dict:
        return {
            "invariants": service.criteria.invariants,
            "domain_criteria": [asdict(c) for c in service.criteria.domain_criteria],
            "layer3": service.criteria.qa.config.as_dict(),
        }

    @mcp.resource("ledger://predictions")
    def predictions() -> list:
        return [asdict(d) for d in service.store.decisions()]

    @mcp.resource("ledger://judge-status")
    def judge_status() -> dict:
        status = service.get_judge_status()
        return {**asdict(status), "recommendation": status.recommendation}

    return mcp


def main() -> int:
    build_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
