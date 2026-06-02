"""Platform detection.

Detects which agentic harness is installed so :mod:`harness_lens.hooks.install`
knows where to write hook/MCP configuration. This Claude Code build detects
Claude Code; additional platforms register here as support lands.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Platform:
    name: str                 # canonical id, e.g. "claude-code"
    label: str                # human label
    settings_path: Path       # file install.py merges hook/MCP config into
    instruction_file: str     # agent-instruction filename (CLAUDE.md, AGENTS.md, ...)
    detected_by: str = ""     # short note on what proved the platform present


def _claude_code() -> Optional[Platform]:
    home = Path.home()
    settings = home / ".claude" / "settings.json"
    has_dir = (home / ".claude").is_dir()
    has_bin = shutil.which("claude") is not None
    if not (has_dir or has_bin):
        return None
    proof = "~/.claude present" if has_dir else "claude binary on PATH"
    return Platform(
        name="claude-code",
        label="Claude Code",
        settings_path=settings,
        instruction_file="CLAUDE.md",
        detected_by=proof,
    )


# Ordered registry of detectors. Codex CLI support appends its own detector here.
_DETECTORS = (_claude_code,)


def detect_all() -> list[Platform]:
    found: list[Platform] = []
    for detector in _DETECTORS:
        platform = detector()
        if platform is not None:
            found.append(platform)
    return found


def detect(name: Optional[str] = None) -> Optional[Platform]:
    """Return the requested platform, or the first detected one when ``name`` is None."""
    platforms = detect_all()
    if name is None:
        return platforms[0] if platforms else None
    for platform in platforms:
        if platform.name == name:
            return platform
    return None
