"""Pillar 1 — Component Observability (file-level, revertible).

AHE only ever edits *external* components the harness controls. Every edit is
backed up first so it can be reverted:

    ~/.harness-lens/components/   working copies of editable components
    ~/.harness-lens/backups/{ts}/ originals captured before each edit

Black-box internals (the agent's own prompt/tools/middleware) are never editable.
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import home_dir

# Editable external components (design §3 ❶). The agent's internals are absent by design.
# Only components with a live application path in this build are listed: editing
# detector.py or a skill file would not affect subsequent runs, so they are not offered
# as evolution targets (they would otherwise yield proposals that cannot be applied).
EDITABLE_COMPONENTS = frozenset({
    "CLAUDE.md",   # Claude Code agent-instruction file
    "AGENTS.md",   # Codex CLI agent-instruction file
    "hooks",
    "qa.py",
})


class ComponentError(RuntimeError):
    pass


@dataclass
class AppliedEdit:
    component: str
    target_path: Path
    backup_path: Optional[Path]  # None when the target did not exist before the edit
    existed: bool
    applied_at: float


class ComponentManager:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or home_dir()
        self.components_dir = self.root / "components"
        self.backups_dir = self.root / "backups"

    def ensure_dirs(self) -> None:
        self.components_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def is_editable(self, component: str) -> bool:
        return component in EDITABLE_COMPONENTS

    def apply(self, component: str, target_path: Path, new_content: str) -> AppliedEdit:
        """Back up ``target_path`` then overwrite it with ``new_content``."""
        if not self.is_editable(component):
            raise ComponentError(
                f"{component!r} is not an editable external component; refusing to modify"
            )
        self.ensure_dirs()
        target_path = Path(target_path)
        existed = target_path.exists()
        backup_path: Optional[Path] = None
        if existed:
            # Unique per edit: two applies of the same filename within one second
            # must not share a backup dir, or the second would overwrite the first
            # original and break rollback of the earlier candidate.
            backup_dir = self.backups_dir / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / target_path.name
            shutil.copy2(target_path, backup_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(new_content, encoding="utf-8")
        return AppliedEdit(
            component=component, target_path=target_path,
            backup_path=backup_path, existed=existed, applied_at=time.time(),
        )

    def revert(self, edit: AppliedEdit) -> None:
        # A file the edit created is removed entirely (true restore of prior state).
        if not edit.existed:
            if edit.target_path.exists():
                edit.target_path.unlink()
            return
        if edit.backup_path is None or not edit.backup_path.exists():
            raise ComponentError(f"backup missing: {edit.backup_path}")
        shutil.copy2(edit.backup_path, edit.target_path)
