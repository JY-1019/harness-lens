"""Tests for the extended harness scan scope: workflows, .cursor/rules, .mcp.json /
mcpServers, plus runtime attribution of the new component kinds."""

from __future__ import annotations

import json
from pathlib import Path

from harness_lens.criteria import ThreeLayerCriteria
from harness_lens.detector import Platform
from harness_lens.harness import inspect_project
from harness_lens.harness_usage import detect_usage


def test_detect_usage_new_kinds():
    def kinds(tool, text):
        return {(u.kind, u.name) for u in detect_usage(tool, text)}

    assert ("workflow", "build") in kinds("Read", "opened .claude/workflows/build.md")
    assert ("cursor_rule", "style") in kinds("Read", "see .cursor/rules/style.mdc")
    assert ("mcp_config", ".mcp.json") in kinds("Bash", "cat .mcp.json")


def _claude_platform(home: Path) -> Platform:
    return Platform(
        name="claude-code", label="Claude Code",
        settings_path=home / ".claude" / "settings.json", instruction_file="CLAUDE.md",
    )


def test_inspect_project_scans_new_components(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    # Project-local scaffolding the scan must now surface.
    (project / ".claude" / "workflows").mkdir(parents=True)
    (project / ".claude" / "workflows" / "review.md").write_text("wf", encoding="utf-8")
    (project / ".cursor" / "rules").mkdir(parents=True)
    (project / ".cursor" / "rules" / "style.mdc").write_text("rule", encoding="utf-8")
    (project / ".mcp.json").write_text("{}", encoding="utf-8")
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"mcpServers": {"a": {}, "b": {}}}), encoding="utf-8"
    )
    (home / ".claude").mkdir(parents=True)

    report = inspect_project(project, _claude_platform(home), ThreeLayerCriteria.load(None),
                             evolution_platform_name="claude-code")
    by_kind = {c.kind for c in report.applied()}
    assert "워크플로우" in by_kind
    assert "Cursor 규칙" in by_kind
    assert any(k.startswith("MCP") for k in by_kind)
    # mcpServers count is surfaced.
    mcp = [c for c in report.applied() if c.kind == "MCP(mcpServers)"]
    assert mcp and "2 개 서버" in mcp[0].detail
    # New scaffolding stays observe-only (not AHE-editable).
    assert all(not c.editable for c in report.applied()
               if c.kind in ("워크플로우", "Cursor 규칙", "MCP(.mcp.json)", "MCP(mcpServers)"))
