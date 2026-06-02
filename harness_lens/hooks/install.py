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
import os
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

# What uvx resolves `--from`. Defaults to the published PyPI name; a local/dev install (the
# package not yet on PyPI) sets HARNESS_LENS_UVX_FROM to a git spec like
# "harness-lens[all] @ git+https://github.com/<owner>/harness-lens.git" so uvx builds from
# source. Read at install time and baked into the static hook/MCP command strings.
_DEFAULT_UVX_FROM = "harness-lens[all]"


def _uvx_from_spec() -> str:
    return os.environ.get("HARNESS_LENS_UVX_FROM", "").strip() or _DEFAULT_UVX_FROM

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

# Codex's hook surface (design Codex §1): no SessionEnd (Stop is the only terminator) and no
# PostToolUseFailure; PreToolUse only intercepts Bash/apply_patch/MCP, the rest fall through to
# PostToolUse or become gaps. Each Codex hook process is told its platform via an env prefix so
# record.py selects CodexReconstructor without re-detecting.
_CODEX_HOOK_EVENTS = (
    ("SessionStart", "session-start", 5),
    ("UserPromptSubmit", "user-prompt", 5),
    ("PreToolUse", "pre-tool", 5),
    ("PostToolUse", "post-tool", 5),
    ("Stop", "stop", 10),
)
_CODEX_PLATFORM_ENV = "HARNESS_LENS_PLATFORM=codex"


@dataclass
class InstallReport:
    platform: str
    settings_path: Path
    runtime_dir: Path
    created_runtime: list[str] = field(default_factory=list)
    merged_hooks: list[str] = field(default_factory=list)
    mcp_registered: bool = False
    settings_backup: Optional[Path] = None
    # Platform-specific advisories shown before the "다음 단계" footer — e.g. Codex's trust
    # requirement and gap caveat, or manual MCP/config.toml steps install cannot do safely.
    notices: list[str] = field(default_factory=list)

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
        for notice in self.notices:
            lines += ["", notice]
        lines += [
            "",
            "다음 단계: harness-lens show / diagnose / evolve / verify / review",
        ]
        return "\n".join(lines)


def _hook_command(subcommand: str, launcher=DEFAULT_LAUNCHER, env_prefix: str = "") -> str:
    command, *rest = launcher
    if command == "uvx":
        # uvx builds a fresh env per run; pull in [all] so the opt-in inline Judge mode
        # (HARNESS_LENS_JUDGE_IN_HOOK=1) has `anthropic` available, mirroring the MCP server.
        parts = [command, "--from", _uvx_from_spec(), "harness-lens", "hook", subcommand]
    else:
        parts = [*launcher, "hook", subcommand]
    # The command string is run by a shell; quote each part so e.g. the `[all]` extra is
    # not glob-expanded (zsh treats `harness-lens[all]` as a character-class pattern).
    command_str = " ".join(shlex.quote(p) for p in parts)
    # An env prefix (e.g. HARNESS_LENS_PLATFORM=codex) is a literal `KEY=value` assignment the
    # shell applies to the command; its value is a fixed token, so it needs no quoting.
    return f"{env_prefix} {command_str}" if env_prefix else command_str


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


def build_codex_hooks(launcher=DEFAULT_LAUNCHER) -> dict:
    hooks: dict = {}
    for event, subcommand, timeout in _CODEX_HOOK_EVENTS:
        command = _hook_command(subcommand, launcher, env_prefix=_CODEX_PLATFORM_ENV)
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if event in _MATCHED_EVENTS:
            entry["matcher"] = ".*"
        hooks[event] = [entry]
    return hooks


def build_mcp_servers(launcher=DEFAULT_LAUNCHER) -> dict:
    command, *rest = launcher
    if command == "uvx":
        # uvx installs an ephemeral env per run; pull in the [all] extra so the
        # server has both `mcp` (to serve) and `anthropic` (so run_diagnosis /
        # propose_evolution tools work), otherwise those tools fail at import.
        args = ["--from", _uvx_from_spec(), "harness-lens", "serve"]
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


def merge_codex_hooks(existing: dict, launcher=DEFAULT_LAUNCHER) -> tuple[dict, list[str]]:
    """Merge harness-lens hook entries into a Codex ``hooks.json`` document.

    Codex's hooks file holds only a ``hooks`` table (MCP lives in config.toml / ``codex mcp``),
    so unlike Claude's settings merge there is no ``mcpServers`` step. Dedup is matcher+command
    aware, identical to the Claude path, so reinstall is idempotent and never duplicates an entry.
    """
    merged = json.loads(json.dumps(existing)) if existing else {}
    hooks = merged.setdefault("hooks", {})
    touched: list[str] = []
    for event, entries in build_codex_hooks(launcher).items():
        bucket = hooks.setdefault(event, [])
        existing_keys = {_entry_key(e) for e in bucket}
        for entry in entries:
            key = _entry_key(entry)
            if key not in existing_keys:
                bucket.append(entry)
                existing_keys.add(key)
                touched.append(event)
    return merged, sorted(set(touched))


def _config_toml_has_hooks(codex_dir: Path) -> bool:
    """Whether ``~/.codex/config.toml`` already defines a ``[hooks]`` table.

    Detected by a line-oriented scan (no tomllib: it is 3.11+ and we target 3.10+, and there is
    no stdlib TOML *writer* to safely merge into anyway). When present, install leaves the TOML
    untouched and tells the user to merge manually rather than risk corrupting their config.
    """
    config = codex_dir / "config.toml"
    if not config.exists():
        return False
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped == "[hooks]" or stripped.startswith("[hooks.") or stripped.startswith("[[hooks"):
            return True
    return False


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


_CODEX_TRUST_NOTICE = (
    "⚠ 중요: Codex 는 trusted 프로젝트에서만 hook 을 로드합니다.\n"
    "   이 프로젝트를 trust 했는지 확인하세요.\n"
    "   (전역 hook 은 ~/.codex/hooks.json 에 설정됨)"
)
_CODEX_GAP_NOTICE = (
    "참고: Codex 는 일부 tool(웹 조사 등)을 hook 으로 잡지 못해\n"
    "   trajectory 에 '관측 불가' 구간이 생길 수 있습니다.\n"
    "   harness-lens status 에서 gap 비율을 확인하세요."
)


def _codex_mcp_notice(launcher) -> str:
    command, *rest = launcher
    serve = " ".join(build_mcp_servers(launcher)["harness-lens"]["args"])
    return (
        "MCP 서버는 자동 등록하지 않았습니다. 다음 중 하나로 등록하세요:\n"
        f"   codex mcp add harness-lens -- {command} {serve}\n"
        "   또는 ~/.codex/config.toml 의 [mcp_servers] 섹션에 직접 추가"
    )


def install(platform_name: Optional[str] = None, launcher=DEFAULT_LAUNCHER, root: Optional[Path] = None) -> InstallReport:
    platform = detect(platform_name)
    if platform is None:
        raise RuntimeError(
            "지원되는 하네스를 찾지 못했습니다 (Claude Code / Codex 미설치). "
            "Claude Code 또는 Codex 설치 후 다시 실행하세요."
        )

    root = root or home_dir()
    created = init_runtime(root)

    if platform.name == "codex":
        return _install_codex(platform, launcher, root, created)

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


def _install_codex(platform: Platform, launcher, root: Path, created: list[str]) -> InstallReport:
    """Codex install path: merge hooks into ~/.codex/hooks.json, never auto-edit config.toml.

    Codex differs from Claude (design Codex §1/§13): hooks live in their own ``hooks.json`` (or
    an inline config.toml ``[hooks]`` table), MCP is registered separately, and project-local
    hooks only load in *trusted* projects — so the report must carry the trust + gap notices.
    """
    hooks_path = platform.settings_path  # ~/.codex/hooks.json
    existing = _load_json(hooks_path)
    merged, touched = merge_codex_hooks(existing, launcher)

    backup_path = None
    if hooks_path.exists():
        manager = ComponentManager(root)
        edit = manager.apply("hooks", hooks_path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        backup_path = edit.backup_path
    else:
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    notices = [_CODEX_TRUST_NOTICE, _CODEX_GAP_NOTICE, _codex_mcp_notice(launcher)]
    if _config_toml_has_hooks(hooks_path.parent):
        # We wrote hooks.json, but the user's config.toml also defines [hooks]. We cannot safely
        # rewrite TOML (no stdlib writer on 3.10+), and which source Codex prefers is ambiguous,
        # so warn them to merge manually rather than leave a silently-shadowed hooks.json.
        notices.insert(0, (
            "⚠ ~/.codex/config.toml 에 이미 [hooks] 가 있습니다.\n"
            "   hooks.json 을 작성했지만, config.toml 의 [hooks] 가 우선할 수 있습니다.\n"
            "   위 hook 항목을 config.toml 에 직접 병합하거나 [hooks] 를 제거하세요."
        ))

    return InstallReport(
        platform=platform.label,
        settings_path=hooks_path,
        runtime_dir=root,
        created_runtime=created,
        merged_hooks=touched,
        mcp_registered=False,
        settings_backup=backup_path,
        notices=notices,
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
