"""Project-scoped harness inspection (design goal §0).

`harness-lens show`/`status` report the *observed* trajectory after the fact.
This module answers the complementary question asked when you run harness-lens
*inside a specific project*: **what harness is actually applied to this project
right now**, and how does AHE see it?

It discovers the external scaffolding a project layers onto Claude Code / Codex —
the agent-instruction file, hook configuration, skills, and prompt/command files —
and abstracts it through the same lens the rest of harness-lens uses:

* the Flow/Task/Step model (which tool maps to which Task category), and
* the three-layer criteria currently governing the project,

while marking which components AHE may actually evolve (the editable external
surface) versus those it only observes. Nothing here mutates anything; it is the
read side of "확인하고 동시에 관리" — the management actions stay in ``evolve`` /
``gui``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .components import EDITABLE_COMPONENTS
from .criteria import ThreeLayerCriteria
from .detector import Platform
from .hooks.install import _config_toml_has_hooks
from .reconstructor import CodexReconstructor, Reconstructor


class LensUnsupportedPlatform(RuntimeError):
    """Raised when no supported harness is detected, so the project's harness
    component layout cannot be resolved."""


@dataclass
class HarnessComponent:
    """One piece of external scaffolding applied to a project."""

    component: str          # canonical name (matches EDITABLE_COMPONENTS where applicable)
    kind: str               # 지시문 | 훅 | 스킬 | 프롬프트
    scope: str              # 프로젝트 | 전역
    path: Path
    exists: bool
    editable: bool          # whether AHE may evolve this component in this build
    detail: str = ""        # short human note (e.g. line/entry count)


@dataclass
class ProjectHarnessReport:
    project_root: Path
    platform_label: str
    platform_name: str
    components: list[HarnessComponent] = field(default_factory=list)
    tool_categories: dict[str, str] = field(default_factory=dict)
    invariant_count: int = 0
    domain_count: int = 0
    layer3: dict = field(default_factory=dict)

    def applied(self) -> list[HarnessComponent]:
        return [c for c in self.components if c.exists]

    def render(self) -> str:
        lines = [
            f"이 프로젝트에 적용된 하네스  [{self.platform_label}]",
            f"  경로: {self.project_root}",
            "",
            "■ 적용된 컴포넌트 (AHE 관점)",
        ]
        applied = self.applied()
        if not applied:
            lines.append("  (이 프로젝트/전역에서 감지된 하네스 컴포넌트가 없습니다)")
        for c in applied:
            tag = "✎ AHE 편집가능" if c.editable else "👁 관측전용"
            extra = f"  — {c.detail}" if c.detail else ""
            lines.append(f"  [{c.scope}] {c.kind}: {c.path}  ({tag}){extra}")

        missing = [c for c in self.components if not c.exists and c.editable]
        if missing:
            lines.append("")
            lines.append("■ 미적용 (AHE 가 생성/편집하는) 컴포넌트")
            for c in missing:
                lines.append(f"  [{c.scope}] {c.kind}: {c.path}  (없음)")

        lines += [
            "",
            "■ Flow/Task/Step 추상화 — 이 프로젝트의 동작을 Task 로 분류하는 규칙",
        ]
        for tool, category in sorted(self.tool_categories.items()):
            lines.append(f"  {tool} → {category}")
        lines.append("  mcp__* → 외부도구")

        layer3 = ", ".join(f"{k}={v}" for k, v in self.layer3.items())
        lines += [
            "",
            "■ 3-Layer 기준 (현재 하네스를 통제)",
            f"  Layer 1 (불변식, 편집 불가): {self.invariant_count} 개",
            f"  Layer 2 (도메인 기준, 사람 관리): {self.domain_count} 개",
            f"  Layer 3 (자동 임계값, AHE 진화 대상): {layer3}",
            "",
            "관리: harness-lens evolve (편집가능 컴포넌트 진화 제안) · "
            "harness-lens status (관측/예측 현황)",
        ]
        return "\n".join(lines)


def _config_dirname(platform: Platform) -> str:
    """The platform's config directory name, e.g. ``.claude`` or ``.codex``."""
    return platform.settings_path.parent.name


def _skill_detail(skills_dir: Path) -> str:
    names = sorted(d.name for d in skills_dir.iterdir() if d.is_dir())
    return f"{len(names)} 개 스킬: {', '.join(names)}" if names else "0 개 스킬"


def _command_detail(commands_dir: Path) -> str:
    files = sorted(
        p.name for p in commands_dir.iterdir()
        if p.is_file() and p.suffix in (".md", ".markdown")
    )
    return f"{len(files)} 개: {', '.join(files)}" if files else "0 개"


def _line_detail(path: Path) -> str:
    try:
        return f"{len(path.read_text(encoding='utf-8').splitlines())} 줄"
    except OSError:
        return ""


def inspect_project(
    project_root: Path,
    platform: Platform,
    criteria: ThreeLayerCriteria,
    evolution_platform_name: Optional[str] = None,
) -> ProjectHarnessReport:
    """Build a :class:`ProjectHarnessReport` for ``project_root`` on ``platform``.

    Both the project-local scaffolding and the global (home) scaffolding are
    discovered, since the harness an agent actually runs under is the union of
    the two.

    ``evolution_platform_name`` is the platform AHE's ``apply_evolution`` would
    actually write to (``LensService._live_target`` resolves through ``detect()``,
    the first installed platform). A component is only flagged AHE-editable when
    the inspected platform matches it; inspecting a non-evolution platform's files
    shows them as observed-only so the report never points edits at the wrong files.
    """
    project_root = Path(project_root).resolve()
    is_evolution_target = (
        evolution_platform_name is not None and platform.name == evolution_platform_name
    )
    config_dirname = _config_dirname(platform)
    instruction = platform.instruction_file
    settings_name = platform.settings_path.name
    home_config = platform.settings_path.parent

    # (scope label, is_global, root for project-root files, config dir for that scope)
    scopes = [
        ("프로젝트", False, project_root, project_root / config_dirname),
        ("전역", True, home_config, home_config),
    ]

    components: list[HarnessComponent] = []
    for scope, is_global, root, config_dir in scopes:
        # AHE's live targets (LensService._live_target) are the *global* instruction file
        # and hook config; a project-local copy still shapes behavior, but evolving it would
        # write to the global file instead. So only global-scope instruction/hooks are
        # advertised as AHE-editable — project-scoped ones are observed-only.
        instr_path = root / instruction
        components.append(HarnessComponent(
            component=instruction, kind="지시문", scope=scope, path=instr_path,
            exists=instr_path.exists(),
            editable=is_global and is_evolution_target and instruction in EDITABLE_COMPONENTS,
            detail=_line_detail(instr_path) if instr_path.exists() else "",
        ))

        # Claude Code also loads hooks from settings.local.json (a gitignored per-project
        # override), so scan it too; Codex only has hooks.json. Only the canonical global
        # settings file is AHE-editable — it is the one LensService._live_target resolves to;
        # a local override is observed but never an evolution target.
        hook_files = [settings_name]
        if platform.name == "claude-code" and not is_global:
            # settings.local.json is a per-project local override under <project>/.claude;
            # it has no home-level meaning, so only scan it in the project scope.
            hook_files.append("settings.local.json")
        for hook_file in hook_files:
            hooks_path = config_dir / hook_file
            components.append(HarnessComponent(
                component="hooks", kind="훅/설정", scope=scope, path=hooks_path,
                exists=hooks_path.exists(),
                editable=(
                    is_global and is_evolution_target
                    and hook_file == settings_name and "hooks" in EDITABLE_COMPONENTS
                ),
                detail="",
            ))

        # Codex can also load hooks from config.toml's [hooks] table, which may take
        # precedence over hooks.json (install.py warns about this). Surface it as an
        # observed-only source so the report does not hide an active hook configuration.
        if platform.name == "codex" and _config_toml_has_hooks(config_dir):
            config_toml = config_dir / "config.toml"
            components.append(HarnessComponent(
                component="hooks", kind="훅/설정(config.toml)", scope=scope, path=config_toml,
                exists=True, editable=False,
                detail="[hooks] 정의됨 — hooks.json 보다 우선할 수 있음",
            ))

        skills_dir = config_dir / "skills"
        if skills_dir.is_dir():
            components.append(HarnessComponent(
                component="skills", kind="스킬", scope=scope, path=skills_dir,
                exists=True, editable=False, detail=_skill_detail(skills_dir),
            ))

        commands_dir = config_dir / "commands"
        if commands_dir.is_dir():
            components.append(HarnessComponent(
                component="commands", kind="프롬프트/커맨드", scope=scope, path=commands_dir,
                exists=True, editable=False, detail=_command_detail(commands_dir),
            ))

    reconstructor_cls = CodexReconstructor if platform.name == "codex" else Reconstructor

    return ProjectHarnessReport(
        project_root=project_root,
        platform_label=platform.label,
        platform_name=platform.name,
        components=components,
        tool_categories=dict(reconstructor_cls.TOOL_CATEGORY),
        invariant_count=len(criteria.invariants),
        domain_count=len(criteria.domain_criteria),
        layer3=criteria.qa.config.as_dict(),
    )
