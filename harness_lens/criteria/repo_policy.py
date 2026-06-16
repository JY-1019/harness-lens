"""Team governance, Phase 1 — a repo-committed policy file the daemon auto-discovers.

Governance is a team concern, but harness-lens is otherwise rooted in each developer's
``~/.harness-lens`` (per-machine, per-user). This module lets a project ship its harness *in the
repo*: a committed ``.harness-lens/policy.yaml`` anywhere up the tree from a session's ``cwd`` is
applied as an additive scope to every session inside that repo. So governance becomes policy-as-code
— distributed by git, reviewed via PR (CODEOWNERS / branch protection), versioned and auditable —
instead of something each developer wires up locally.

Composition (see :func:`~harness_lens.criteria.scope.apply_scope`, which is pure + additive):
``personal base  →  repo policy (this module)  →  personal scope overlay``. The repo policy's
invariants/criteria are *added*, its ``layer3`` overrides, and its optional ``mode`` pins the project.
The personal home overlay still applies on top for local tuning. (True non-bypassable authority —
the individual cannot weaken org rules — needs a server/CI gate; that is a later phase. This phase
delivers the distribution + review + version-control story, which is the bulk of the value.)

File shape (flat layers, same vocabulary as ``criteria.yaml``)::

    # <repo>/.harness-lens/policy.yaml   (committed, team-owned)
    invariants:
      - "프로덕션 DB에 직접 DELETE 를 실행하지 않는다"
    domain_criteria:
      - id: BIZ-001
        description: "결제·정산 로직 변경 시 회귀 테스트를 동반한다"
        weight: 1.5
    layer3:
      failure_count_trigger: 2
    mode: enforce        # optional — pin this repo to observe/enforce
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .scope import Scope, parse_scopes

# A repo declares its harness in <repo>/.harness-lens/policy.yaml.
REPO_POLICY_DIR = ".harness-lens"
REPO_POLICY_FILE = "policy.yaml"


def find_repo_policy(cwd: Optional[str]) -> Optional[Path]:
    """The nearest ancestor ``.harness-lens/policy.yaml`` from ``cwd`` (inclusive), or None.

    Walks up to the filesystem root so a session in any subdirectory of the repo is governed by the
    repo's committed policy. Returns None for a missing/unreadable ``cwd``.
    """
    if not cwd:
        return None
    try:
        current = Path(cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for directory in (current, *current.parents):
        candidate = directory / REPO_POLICY_DIR / REPO_POLICY_FILE
        if candidate.is_file():
            return candidate
    return None


def repo_root_of(policy_path: Path) -> str:
    """The repo root governed by ``policy_path`` (the dir that holds ``.harness-lens/``)."""
    return str(Path(policy_path).parent.parent)


def load_repo_policy(policy_path: Path) -> Optional[Scope]:
    """Parse a flat repo policy file into a :class:`Scope` keyed to its repo root.

    The flat ``invariants / domain_criteria / layer3 / mode`` shape is mapped onto a ``cwd_prefix``
    scope (matching the whole repo) and validated through :func:`parse_scopes`, so it reuses the same
    sanitisation as GUI/API scopes. Never raises — a missing/empty/invalid file yields None.
    """
    path = Path(policy_path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    root = repo_root_of(path)
    raw = {
        "name": str(data.get("name") or f"repo:{Path(root).name}"),
        "match": {"cwd_prefix": root},
        "add_invariants": data.get("invariants") or [],
        "add_domain_criteria": data.get("domain_criteria") or [],
        "layer3": data.get("layer3") if isinstance(data.get("layer3"), dict) else {},
    }
    mode = data.get("mode")
    if mode in ("observe", "enforce"):
        raw["mode"] = mode
    scopes = parse_scopes([raw])
    return scopes[0] if scopes else None
