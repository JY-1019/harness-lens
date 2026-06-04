"""SKILL wrapping — let the harness invoke harness-lens as a skill, not only a CLI.

A user shouldn't have to remember the CLI surface: ``install`` also drops a skill
the host agent can trigger which wraps the same ``harness-lens`` subcommands. Both
Claude Code and Codex discover skills the same way — a ``SKILL.md`` with YAML
frontmatter (``name`` + ``description``) under ``<config>/skills/<name>/`` — so the
body is generated from a single source, with the actual invocation prefix
(``harness-lens`` vs ``uvx --from … harness-lens``) injected by the caller so the
skill works whether or not the package is installed globally.
"""

from __future__ import annotations

from pathlib import Path

from .detector import Platform

SKILL_NAME = "harness-lens"

# Per-platform config directory that holds the ``skills/`` tree.
_CONFIG_DIR = {"claude-code": ".claude", "codex": ".codex"}

# One-line description used as the skill's trigger hint (Claude frontmatter / Codex header).
_DESCRIPTION = (
    "Observe and evolve the agentic harness. Use when the user wants to inspect "
    "Flows/Tasks/Steps, see the project's harness, diagnose failure patterns, "
    "propose or apply harness evolutions, verify predictions, or check harness status."
)

# (subcommand-with-args, what it does) — the canonical command catalogue the skill exposes.
_COMMANDS = (
    ("show [--fail] [--limit N]", "recent Flows with Layer-2 scores and gap ratio"),
    ("harness [--project DIR]", "inspect the harness applied to this project (components + 3-Layer)"),
    ("status", "3-Layer state, prediction hit-rate, Judge recommendation, gap ratio"),
    ("diagnose", "Pillar 2 — diagnose recurring failure patterns (needs an LLM backend)"),
    ("evolve", "Pillar 3 — propose harness fixes, each with a falsifiable prediction"),
    ("evolve --apply ID --yes", "apply a proposed candidate (backs up the original first)"),
    ("verify", "check applied predictions → confirm hits, roll back misses"),
    ("review [--sample ID --label 0..1]", "label Judge samples to calibrate Layer-2"),
    ("rollback", "revert the last applied change"),
)


def _command_lines(invoke: str) -> str:
    return "\n".join(f"- `{invoke} {cmd}` — {desc}" for cmd, desc in _COMMANDS)


def render(platform: Platform, invoke: str = SKILL_NAME) -> str:
    """Return the ``SKILL.md`` body for ``platform``, calling harness-lens via ``invoke``.

    ``invoke`` is the command prefix before the subcommand (e.g. ``harness-lens`` or
    ``uvx --from 'harness-lens[all]' harness-lens``) so the generated skill matches how
    the host was actually wired. Both Claude Code and Codex require YAML frontmatter
    (``name`` + ``description``) followed by the markdown body.
    """
    # Pin the platform so commands that resolve it via detect() (harness/diagnose/evolve and
    # the LLM backend) operate on the harness this skill was installed for, not the
    # registry-first default — otherwise a Codex skill on a dual-install machine would drive
    # Claude. HARNESS_LENS_PLATFORM is honoured by detect(); the value is a fixed token, no quoting.
    bound_invoke = f"HARNESS_LENS_PLATFORM={platform.name} {invoke}"
    front = f"---\nname: {SKILL_NAME}\ndescription: {_DESCRIPTION}\n---\n\n"
    body = (
        f"# {SKILL_NAME}\n\n"
        f"{_DESCRIPTION}\n\n"
        "harness-lens watches this harness through hooks and an MCP server and reconstructs "
        "each session into a Flow / Task / Step trajectory. Run the relevant command and relay "
        "its output; do not invent Flow data.\n\n"
        "## Commands\n\n"
        f"{_command_lines(bound_invoke)}\n\n"
        "## Guidance\n\n"
        "- For \"what happened\" / \"show recent work\" use `show`; add `--fail` for only failed Flows.\n"
        "- For \"what's wrong with the harness\" run `diagnose`, then `evolve` to get fix proposals.\n"
        "- Only apply an evolution with `evolve --apply` after the user confirms; it edits real "
        "files (CLAUDE.md / AGENTS.md / hooks / qa.py) and is reverted by `rollback`.\n"
        "- `diagnose` / `evolve` need an LLM backend; if none is configured, say so instead of guessing.\n"
    )
    return front + body


def skill_path(platform: Platform, home: Path | None = None) -> Path:
    """Where the skill file lives for ``platform`` (global scope, ``<config>/skills/<name>/SKILL.md``)."""
    home = home or Path.home()
    config_dir = _CONFIG_DIR.get(platform.name, f".{platform.name}")
    return home / config_dir / "skills" / SKILL_NAME / "SKILL.md"


def install_skill(platform: Platform, invoke: str = SKILL_NAME, home: Path | None = None) -> tuple[Path, bool]:
    """Write the skill for ``platform``. Returns ``(path, changed)``; ``changed`` is False if up to date."""
    path = skill_path(platform, home)
    content = render(platform, invoke)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path, True
