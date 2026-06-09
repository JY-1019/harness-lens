"""Per-scope policy resolution — project/session-scoped 3-Layer criteria.

The harness is one global 3-Layer base. A *scope* lets a specific project (matched by the
session's ``cwd`` prefix) or a specific session run under criteria that differ from that base:

* ``mode`` — pin ``observe``/``enforce`` for this scope (e.g. "this project is always enforce"),
  overriding the daemon's global mode.
* ``layer3`` — override Layer-3 QA thresholds (stricter retries/latency/quality here).
* ``add_invariants`` / ``add_domain_criteria`` — *additively* extend Layer 1 / Layer 2. A scope can
  only **tighten** the harness, never remove a global rule: Layer 1 is "절대 위반 금지" everywhere, so
  the safety floor is the same for every session and scopes layer extra rules on top. (Added
  invariants enforce only when their text maps to a built-in invariant detector; otherwise, like any
  detector-less rule, they are advisory — same semantics as the global list.)

Scopes are declared in ``criteria.yaml`` under a ``scopes:`` list::

    scopes:
      - name: payments
        match: { cwd_prefix: "/Users/me/work/payments" }
        mode: enforce
        layer3: { retry_threshold: 1, quality_threshold: 0.95 }
        add_invariants:
          - "프로덕션 DB에 직접 DELETE를 실행하지 않는다"

Resolution picks the most specific match: an exact ``session_id`` beats any path, otherwise the
longest matching ``cwd_prefix`` wins; no match falls back to the global base.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .domain import DomainCriterion
from .layer import ThreeLayerCriteria
from .qa import QACriteria, QAConfig

_MODES = ("observe", "enforce")
# Beats any cwd_prefix length, so an exact session match always wins resolution.
_SESSION_MATCH_SCORE = 1_000_000


@dataclass
class Scope:
    name: str
    cwd_prefix: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None  # observe | enforce | None (inherit the global mode)
    add_invariants: list[str] = field(default_factory=list)
    add_domain_criteria: list[DomainCriterion] = field(default_factory=list)
    layer3: dict = field(default_factory=dict)

    def match_score(self, cwd: Optional[str], session_id: Optional[str]) -> int:
        """Specificity of this scope's match (higher = more specific), or -1 if it does not apply."""
        if self.session_id and session_id and self.session_id == session_id:
            return _SESSION_MATCH_SCORE
        if self.cwd_prefix and cwd and _path_has_prefix(cwd, self.cwd_prefix):
            return len(_norm(self.cwd_prefix))
        return -1


def _norm(p: str) -> str:
    return Path(p).as_posix().rstrip("/")


def _path_has_prefix(cwd: str, prefix: str) -> bool:
    """Prefix test on path boundaries so ``/a/proj`` does not match prefix ``/a/project``."""
    c, p = _norm(cwd), _norm(prefix)
    return c == p or c.startswith(p + "/")


def load_scopes(path: Optional[Path]) -> list[Scope]:
    """Parse the optional ``scopes:`` list from criteria.yaml. Malformed entries are skipped."""
    if path is None or not Path(path).exists():
        return []
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return []
    out: list[Scope] = []
    for i, raw in enumerate(data.get("scopes") or []):
        if not isinstance(raw, dict):
            continue
        match = raw.get("match") or {}
        if not isinstance(match, dict):
            match = {}
        mode = raw.get("mode")
        if mode not in _MODES:
            mode = None
        cwd_prefix = match.get("cwd_prefix")
        session_id = match.get("session_id")
        if not cwd_prefix and not session_id:
            continue  # a scope with nothing to match on can never apply — drop it
        domain = []
        for d in raw.get("add_domain_criteria") or []:
            if isinstance(d, dict) and d.get("id"):
                domain.append(DomainCriterion.from_dict(d))
        out.append(Scope(
            name=str(raw.get("name") or cwd_prefix or session_id or f"scope-{i + 1}"),
            cwd_prefix=str(cwd_prefix) if cwd_prefix else None,
            session_id=str(session_id) if session_id else None,
            mode=mode,
            add_invariants=[str(x) for x in (raw.get("add_invariants") or [])],
            add_domain_criteria=domain,
            layer3=dict(raw.get("layer3") or {}),
        ))
    return out


def resolve_scope(scopes: list[Scope], cwd: Optional[str],
                  session_id: Optional[str]) -> Optional[Scope]:
    """Return the most specific scope that applies to ``(cwd, session_id)``, or None."""
    best: Optional[Scope] = None
    best_score = -1
    for scope in scopes:
        score = scope.match_score(cwd, session_id)
        if score > best_score:
            best, best_score = scope, score
    return best


def apply_scope(base: ThreeLayerCriteria, scope: Optional[Scope]) -> ThreeLayerCriteria:
    """Build the effective criteria for ``scope`` layered over ``base`` (pure — base is untouched)."""
    if scope is None:
        return base
    invariants = list(base.invariants) + list(scope.add_invariants)
    domain = list(base.domain_criteria) + list(scope.add_domain_criteria)
    # Start from the base's *effective* Layer 3 (config + any active overrides), then apply the
    # scope's overrides for known keys only — an unknown/out-of-range key is ignored, not fatal.
    merged = {k: base.qa.effective(k) for k in QACriteria.EVOLVABLE_KEYS}
    for key, value in scope.layer3.items():
        if key in QACriteria.EVOLVABLE_KEYS:
            merged[key] = value
    qa = QACriteria(QAConfig.from_dict(merged))
    return ThreeLayerCriteria(invariants=invariants, domain_criteria=domain, qa=qa)
