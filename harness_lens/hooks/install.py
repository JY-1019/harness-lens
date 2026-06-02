"""Installer — wire harness-lens into Claude Code and initialise the runtime.

* Detects Claude Code (:mod:`harness_lens.detector`).
* Merges ``mcpServers`` + ``hooks`` into ``~/.claude/settings.json`` (existing
  settings are preserved and backed up first).
* Initialises ``~/.harness-lens/`` (ledger.db, criteria.yaml, components/, backups/).

The hook set extends design §13's minimal example with ``PreToolUse`` (needed to
open a Step, per §2/§16) and ``Stop`` (a Flow-end candidate).
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import home_dir
from ..components import ComponentManager
from ..criteria import DEFAULT_CRITERIA_YAML
from ..detector import Platform, detect
from ..store import SQLiteStore

# How hooks/MCP invoke harness-lens. Overridable for local/dev installs.
DEFAULT_LAUNCHER = ("uvx", "harness-lens")

# (settings.json event name, hook subcommand, timeout seconds)
_HOOK_EVENTS = (
    ("SessionStart", "session-start", 5),
    ("UserPromptSubmit", "user-prompt", 5),
    ("PreToolUse", "pre-tool", 5),
    ("PostToolUse", "post-tool", 5),
    # Claude Code fires PostToolUseFailure (not PostToolUse) when a tool fails; without it
    # a failed call would stay an open success=None step and the failure go unrecorded.
    ("PostToolUseFailure", "post-tool-failure", 5),
    ("Stop", "stop", 5),
    ("SessionEnd", "session-end", 10),
)
_MATCHED_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


@dataclass
class InstallReport:
    platform: str
    settings_path: Path
    runtime_dir: Path
    created_runtime: list[str] = field(default_factory=list)
    merged_hooks: list[str] = field(default_factory=list)
    mcp_registered: bool = False
    settings_backup: Optional[Path] = None

    def render(self) -> str:
        lines = [
            f"✅ harness-lens 설치 완료 ({self.platform})",
            f"   settings: {self.settings_path}",
            f"   runtime : {self.runtime_dir}",
        ]
        if self.created_runtime:
            lines.append(f"   created : {', '.join(self.created_runtime)}")
        if self.merged_hooks:
            lines.append(f"   hooks   : {', '.join(self.merged_hooks)}")
        lines.append(f"   mcp     : {'registered' if self.mcp_registered else 'unchanged'}")
        if self.settings_backup:
            lines.append(f"   backup  : {self.settings_backup}")
        lines += [
            "",
            "다음 단계: harness-lens show / diagnose / evolve / verify / review",
        ]
        return "\n".join(lines)


def _hook_command(subcommand: str, launcher=DEFAULT_LAUNCHER) -> str:
    command, *rest = launcher
    if command == "uvx":
        # uvx builds a fresh env per run; pull in [all] so the opt-in inline Judge mode
        # (HARNESS_LENS_JUDGE_IN_HOOK=1) has `anthropic` available, mirroring the MCP server.
        parts = [command, "--from", "harness-lens[all]", "harness-lens", "hook", subcommand]
    else:
        parts = [*launcher, "hook", subcommand]
    # The command string is run by a shell; quote each part so e.g. the `[all]` extra is
    # not glob-expanded (zsh treats `harness-lens[all]` as a character-class pattern).
    return " ".join(shlex.quote(p) for p in parts)


def build_hooks(launcher=DEFAULT_LAUNCHER) -> dict:
    hooks: dict = {}
    for event, subcommand, timeout in _HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": _hook_command(subcommand, launcher), "timeout": timeout}]}
        if event in _MATCHED_EVENTS:
            # Claude Code matchers are tool-name regex patterns; a bare "*" is not a
            # valid match-all and would silently capture nothing. ".*" matches all tools.
            entry["matcher"] = ".*"
        hooks[event] = [entry]
    return hooks


def build_mcp_servers(launcher=DEFAULT_LAUNCHER) -> dict:
    command, *rest = launcher
    if command == "uvx":
        # uvx installs an ephemeral env per run; pull in the [all] extra so the
        # server has both `mcp` (to serve) and `anthropic` (so run_diagnosis /
        # propose_evolution tools work), otherwise those tools fail at import.
        args = ["--from", "harness-lens[all]", "harness-lens", "serve"]
    else:
        args = [*rest, "serve"]
    return {"harness-lens": {"command": command, "args": args}}


def _command_of(entry: dict) -> str:
    for hook in entry.get("hooks", []):
        if hook.get("type") == "command":
            return hook.get("command", "")
    return ""


def _entry_key(entry: dict) -> tuple[str, str]:
    """Identity of a hook entry: matcher + command.

    Keying on command alone would let an existing narrow-matcher entry (or one with no
    matcher) suppress our ``matcher: ".*"`` entry on reinstall, leaving most tools
    unobserved. Including the matcher means the broad entry is added instead.
    """
    return (entry.get("matcher", ""), _command_of(entry))


def merge_settings(existing: dict, launcher=DEFAULT_LAUNCHER) -> tuple[dict, list[str], bool]:
    """Return ``(merged, hook_events_touched, mcp_registered)`` without mutating ``existing``."""
    merged = json.loads(json.dumps(existing)) if existing else {}

    servers = merged.setdefault("mcpServers", {})
    mcp_registered = "harness-lens" not in servers
    for name, spec in build_mcp_servers(launcher).items():
        current = servers.get(name)
        if isinstance(current, dict):
            # Refresh command/args to the current launcher but keep any user-added env,
            # model/home overrides, or custom launcher fields so reinstall is non-destructive.
            current.update(spec)
        else:
            servers[name] = spec

    hooks = merged.setdefault("hooks", {})
    touched: list[str] = []
    for event, entries in build_hooks(launcher).items():
        bucket = hooks.setdefault(event, [])
        existing_keys = {_entry_key(e) for e in bucket}
        for entry in entries:
            key = _entry_key(entry)
            if key not in existing_keys:
                bucket.append(entry)
                existing_keys.add(key)
                touched.append(event)
    return merged, touched, mcp_registered


def init_runtime(root: Optional[Path] = None) -> list[str]:
    root = root or home_dir()
    created: list[str] = []
    root.mkdir(parents=True, exist_ok=True)

    manager = ComponentManager(root)
    manager.ensure_dirs()
    created += ["components/", "backups/"]

    criteria_path = root / "criteria.yaml"
    if not criteria_path.exists():
        criteria_path.write_text(DEFAULT_CRITERIA_YAML, encoding="utf-8")
        created.append("criteria.yaml")

    SQLiteStore(root / "ledger.db").close()  # creates + migrates schema
    created.append("ledger.db")
    return created


def install(platform_name: Optional[str] = None, launcher=DEFAULT_LAUNCHER, root: Optional[Path] = None) -> InstallReport:
    platform = detect(platform_name)
    if platform is None:
        raise RuntimeError(
            "지원되는 하네스를 찾지 못했습니다 (Claude Code 미설치). "
            "Claude Code 설치 후 다시 실행하세요."
        )

    root = root or home_dir()
    created = init_runtime(root)

    settings_path = platform.settings_path
    existing = _load_json(settings_path)
    merged, touched, mcp_registered = merge_settings(existing, launcher)

    backup_path = None
    if settings_path.exists():
        manager = ComponentManager(root)
        edit = manager.apply("hooks", settings_path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        backup_path = edit.backup_path
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return InstallReport(
        platform=platform.label,
        settings_path=settings_path,
        runtime_dir=root,
        created_runtime=created,
        merged_hooks=sorted(set(touched)),
        mcp_registered=mcp_registered,
        settings_backup=backup_path,
    )


def _strip_trailing_commas(text: str) -> str:
    """Drop commas that precede ``}``/``]``, but only *outside* string literals.

    A single regex over the whole text would also rewrite a string value that happens to
    contain ``,}`` (e.g. a hook command embedding JSON), silently corrupting the user's
    settings. Tracking string state here keeps such values intact.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # trailing comma → drop it, keep the following whitespace
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` / ``/* */`` comments and trailing commas, respecting string literals.

    Claude Code accepts JSONC in ``settings.json``; only ``"``-delimited strings are JSON
    strings, so comment markers and commas inside them are left untouched.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return _strip_trailing_commas("".join(out))


def loads_jsonc(text: str) -> dict:
    """Parse strict JSON, falling back to a JSONC (comments/trailing commas) tolerant parse.

    Raises ``json.JSONDecodeError`` when neither parses, so callers abort instead of
    silently treating an unreadable settings file as empty and overwriting it.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_jsonc(text))


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    # Abort (propagate) on a non-empty but unparseable file rather than returning {} —
    # install() would otherwise rewrite the live settings with only harness-lens entries,
    # dropping the user's existing config.
    data = loads_jsonc(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data
