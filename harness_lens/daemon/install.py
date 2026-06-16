"""Install the daemon hook wiring — ``harness-lens install --enforce|--observe``.

Unlike the legacy observe-only install (which wires short-lived ``hook <event>`` processes),
this registers the **relay** command on every lifecycle event: each hook posts to the single
daemon and relays its response, so control events can deny/escalate/allow. It also persists the
starting ``mode`` and ensures the auth token exists.

Hook registration reuses the legacy installer's settings-merge primitives (idempotent,
matcher+command keyed, backed up first) so an existing ``settings.json`` is never clobbered.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import home_dir
from ..components import ComponentManager
from ..criteria import DEFAULT_CRITERIA_YAML, ThreeLayerCriteria
from ..detector import Platform, detect
from ..hooks.install import (
    DEFAULT_LAUNCHER,
    _cli_invocation,
    _entry_key,
    _enforce_three_layer,
    _load_json,
    _uvx_from_spec,
)
from ..skill import install_skill
from .config import MODE_ENFORCE, MODE_OBSERVE, DaemonConfig, ensure_token

# Native hook events to register. Control events (PreToolUse/Stop/SubagentStop/UserPromptSubmit)
# carry decisions; the rest are observe-only but still relayed so the daemon reconstructs the tree.
_CLAUDE_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "SubagentStop", "Stop", "Notification", "PreCompact", "SessionEnd",
)
_CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_MATCHED = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}

# A control event may block for the full approval window; give its hook a generous timeout so the
# harness does not kill the relay before a human resolves the escalation.
_CONTROL_TIMEOUT = 90
_OBSERVE_TIMEOUT = 10


def _relay_command(source: str, launcher=DEFAULT_LAUNCHER) -> str:
    command, *rest = launcher
    if command == "uvx":
        parts = [command, "--from", _uvx_from_spec(), "harness-lens", "hook-relay", source]
    else:
        parts = [*launcher, "hook-relay", source]
    return " ".join(shlex.quote(p) for p in parts)


def build_relay_hooks(source: str, events, launcher=DEFAULT_LAUNCHER) -> dict:
    command = _relay_command(source, launcher)
    hooks: dict = {}
    for event in events:
        timeout = _CONTROL_TIMEOUT if event in ("PreToolUse", "Stop", "SubagentStop", "UserPromptSubmit") else _OBSERVE_TIMEOUT
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if event in _MATCHED:
            entry["matcher"] = ".*"
        hooks[event] = [entry]
    return hooks


def _merge_hooks(existing: dict, hooks: dict) -> tuple[dict, list[str]]:
    merged = json.loads(json.dumps(existing)) if existing else {}
    bucket_root = merged.setdefault("hooks", {})
    touched: list[str] = []
    for event, entries in hooks.items():
        bucket = bucket_root.setdefault(event, [])
        keys = {_entry_key(e) for e in bucket}
        for entry in entries:
            key = _entry_key(entry)
            if key not in keys:
                bucket.append(entry)
                keys.add(key)
                touched.append(event)
    return merged, sorted(set(touched))


@dataclass
class DaemonInstallReport:
    platform: str
    mode: str
    settings_path: Path
    runtime_dir: Path
    merged_hooks: list[str] = field(default_factory=list)
    enforced_path: Optional[Path] = None
    skill_path: Optional[Path] = None
    notices: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"✅ harness-lens daemon 설치 완료 ({self.platform}, mode={self.mode})",
            f"   settings: {self.settings_path}",
            f"   runtime : {self.runtime_dir}",
            f"   hooks   : {', '.join(self.merged_hooks) or '변경 없음'}",
        ]
        if self.enforced_path:
            lines.append(f"   3-layer : {self.enforced_path}")
        if self.skill_path:
            lines.append(f"   skill   : {self.skill_path}")
        for n in self.notices:
            lines += ["", n]
        lines += ["", "다음: harness-lens daemon start  →  harness-lens status / approvals / tail"]
        return "\n".join(lines)


def _init_daemon_runtime(root: Path) -> None:
    """Create the runtime dirs + criteria.yaml without touching ledger.db (the daemon owns it)."""
    root.mkdir(parents=True, exist_ok=True)
    ComponentManager(root).ensure_dirs()
    criteria_path = root / "criteria.yaml"
    if not criteria_path.exists():
        criteria_path.write_text(DEFAULT_CRITERIA_YAML, encoding="utf-8")
    ensure_token(root)


def install_daemon(
    mode: str = MODE_OBSERVE,
    platform_name: Optional[str] = None,
    launcher=DEFAULT_LAUNCHER,
    root: Optional[Path] = None,
) -> DaemonInstallReport:
    if mode not in (MODE_OBSERVE, MODE_ENFORCE):
        raise ValueError(f"mode must be observe|enforce (got {mode!r})")
    platform = detect(platform_name)
    if platform is None:
        raise RuntimeError(
            "지원되는 하네스를 찾지 못했습니다 (Claude Code / Codex 미설치). 설치 후 다시 실행하세요."
        )
    root = root or home_dir()
    _init_daemon_runtime(root)
    cfg = DaemonConfig.load(root)
    cfg.mode = mode
    cfg.save(root)

    source = "claude_code" if platform.name == "claude-code" else "codex"
    events = _CLAUDE_EVENTS if source == "claude_code" else _CODEX_EVENTS
    settings_path = platform.settings_path
    existing = _load_json(settings_path)
    merged, touched = _merge_hooks(existing, build_relay_hooks(source, events, launcher))

    if settings_path.exists():
        edit = ComponentManager(root).apply(
            "hooks", settings_path, json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        )
        _ = edit
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    skill_file, _ = install_skill(platform, invoke=_cli_invocation(launcher))
    enforced = _enforce_three_layer(platform, root)

    notices = []
    if source == "codex":
        notices.append(
            "참고: Codex(≥0.139)는 Claude Code 와 동일한 hook 출력 스키마(permissionDecision)를 사용합니다.\n"
            "   trusted 프로젝트에서만 hook 을 로드하므로 ~/.codex/config.toml 의 trust_level 을 확인하세요."
        )
    return DaemonInstallReport(
        platform=platform.label, mode=mode, settings_path=settings_path, runtime_dir=root,
        merged_hooks=touched, enforced_path=enforced, skill_path=skill_file, notices=notices,
    )
