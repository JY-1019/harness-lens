"""Detect where user-authored harness components are exercised during a run.

The platforms inject CLAUDE.md / AGENTS.md into the system prompt with no per-step event, and a
skill is usually consulted as a file read rather than a dedicated tool call (the ledger shows
``Read .claude/skills/<name>/SKILL.md``, not a ``Skill`` tool step). So "which harness acted at
this step" is not delivered by hooks. This recovers it heuristically from each step's tool name
and input/output text: an explicit invocation tool, an ``mcp__`` server, or a reference to a known
harness path. It is best-effort *attribution of where a harness prompt entered the run* — not a
claim that the component changed the model's behaviour (that is unobservable through hooks).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Tools whose very invocation is a harness component activating (Claude Code surfaces these when
# they fire a PreToolUse; older flows used file reads instead, caught by the path patterns below).
_INVOKE_TOOLS = {"Skill", "SlashCommand", "Task", "Agent"}

# A captured name must look like a real component id, not regex/shell fragments from a step that
# happened to contain a path pattern (e.g. editing this very file's regex). Rejects "([^", etc.
_VALID_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

# (kind, compiled pattern) — first capture group is the component name.
_REF_PATTERNS = [
    ("skill", re.compile(r'(?:\.claude|\.codex)/skills/([^/"\\\s]+)')),
    ("command", re.compile(r'(?:\.claude|\.codex)/(?:commands|prompts)/([^/"\\\s.]+)')),
    ("workflow", re.compile(r'(?:\.claude|\.codex)/workflows/([^/"\\\s.]+)')),
    ("plugin", re.compile(r'\.claude/plugins/([^/"\\\s]+)')),
    # Cursor project rules (`.cursor/rules/<name>.mdc`). Cursor is not a hooked platform, but its
    # rule files are harness scaffolding that shapes a run, so attribute them where they appear.
    ("cursor_rule", re.compile(r'\.cursor/rules/([^/"\\\s.]+)')),
    ("instruction", re.compile(r'\b(CLAUDE\.md|AGENTS\.md)\b')),
    # A standalone MCP config file referenced in a step (project `.mcp.json`).
    ("mcp_config", re.compile(r'(?<![\w.])(\.mcp\.json)\b')),
]


@dataclass(frozen=True)
class HarnessUsage:
    kind: str  # invoke | mcp | skill | command | plugin | instruction
    name: str


def detect_usage(tool_name: str, input_summary: str = "", output_summary: str = "") -> list[HarnessUsage]:
    """Return the harness components a single step exercised (deduped, in detection order)."""
    text = f"{tool_name}\n{input_summary}\n{output_summary}"
    seen: list[HarnessUsage] = []

    def add(kind: str, name: str) -> None:
        u = HarnessUsage(kind, name)
        if u not in seen:
            seen.append(u)

    if tool_name in _INVOKE_TOOLS:
        add("invoke", tool_name)
    if tool_name.startswith("mcp__"):
        add("mcp", tool_name)
    for kind, pattern in _REF_PATTERNS:
        for match in pattern.findall(text):
            if _VALID_NAME.match(match):
                add(kind, match)
    return seen


# A component's *prompt file* declares the prompts it intends to use in a different vocabulary
# than a trajectory: slash-command tokens (``/codex-review-step``), Claude Code ``@imports``,
# plugin-cache paths, and bare script names — not the concrete file reads/tool calls that
# _REF_PATTERNS matches in a step's I/O. These patterns recover that *declared* graph.
_DECL_PATTERNS = [
    # Slash-command invocation in prose: `/codex-review-step`. Require a non-word, non-path
    # char before the slash so it is a command token, not the tail of a path (`a/b`) or regex.
    ("command", re.compile(r'(?<![\w./])/([a-z][a-z0-9_-]{1,63})\b')),
    # Claude Code @import of another prompt/instruction file: `@.claude/skills/x/SKILL.md`.
    ("import", re.compile(r'@([A-Za-z0-9._/-]+\.md)\b')),
    # A plugin referenced through its cache path: `.../plugins/cache/<plugin>/...`.
    ("plugin", re.compile(r'plugins/cache/([^/"\\\s]+)')),
    # A referenced executable prompt/script by basename: `codex-companion.mjs`, `foo.sh`.
    ("script", re.compile(r'\b([A-Za-z0-9._-]+\.(?:mjs|js|sh|py|ts))\b')),
]

# Generic path segments the patterns above can capture but which are not real component ids.
_DECL_IGNORE = {("plugin", "cache")}

# Config roots a component's prompt file may live under, searched in order (global first).
_CONFIG_DIRS = (".claude", ".codex")


def _component_file(kind: str, name: str) -> Optional[Path]:
    """Locate the on-disk prompt file backing a (kind, name) component, if any.

    Only skills and slash commands have a single canonical prose file we can statically
    parse for the prompts they reference; everything else (mcp/plugin/instruction/invoke)
    returns ``None``. Searches the user's global config dirs and the cwd project dirs,
    first match wins.
    """
    for base in (Path.home(), Path.cwd()):
        for cfg in _CONFIG_DIRS:
            root = base / cfg
            if kind == "skill":
                candidate = root / "skills" / name / "SKILL.md"
                if candidate.is_file():
                    return candidate
            if kind == "command":
                for sub in ("commands", "prompts"):
                    candidate = root / sub / f"{name}.md"
                    if candidate.is_file():
                        return candidate
    return None


def declared_references(kind: str, name: str) -> Optional[dict]:
    """Statically parse a component's prompt file for the prompts it declares it will use.

    Returns ``{"path": str, "references": list[HarnessUsage]}`` for a resolvable skill/command,
    or ``None`` when no prose file backs the component. Self-references are dropped. This is the
    *declared* half of the declared-vs-fired view: it says what a Skill points at, not what the
    run actually exercised (that is :func:`detect_usage` over the trajectory).
    """
    path = _component_file(kind, name)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    refs: list[HarnessUsage] = []
    seen: set[HarnessUsage] = set()

    def add(k: str, n: str) -> None:
        if (k, n) in _DECL_IGNORE or (k == kind and n == name):
            return
        u = HarnessUsage(k, n)
        if u not in seen:
            seen.add(u)
            refs.append(u)

    for patterns in (_REF_PATTERNS, _DECL_PATTERNS):
        for k, pattern in patterns:
            for match in pattern.findall(text):
                if _VALID_NAME.match(match):
                    add(k, match)
    return {"path": str(path), "references": refs}
