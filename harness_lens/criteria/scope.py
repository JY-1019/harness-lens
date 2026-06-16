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
from .qa import QACriteria, QAConfig, layer3_in_range

_MODES = ("observe", "enforce")
# Beats any cwd match, so an exact session match always wins resolution.
_SESSION_MATCH_SCORE = 1_000_000
# An exact-folder (cwd ==) match beats any prefix match, so a project's own harness wins over a
# broader parent-folder prefix scope, but still loses to a per-session pin.
_EXACT_CWD_SCORE = 500_000


@dataclass
class Scope:
    name: str
    cwd_prefix: Optional[str] = None
    cwd: Optional[str] = None  # exact-folder match (cwd ==) — the per-project harness key
    session_id: Optional[str] = None
    mode: Optional[str] = None  # observe | enforce | None (inherit the global mode)
    add_invariants: list[str] = field(default_factory=list)
    add_domain_criteria: list[DomainCriterion] = field(default_factory=list)
    layer3: dict = field(default_factory=dict)

    def match_score(self, cwd: Optional[str], session_id: Optional[str]) -> int:
        """Specificity of this scope's match (higher = more specific), or -1 if it does not apply."""
        if self.session_id and session_id and self.session_id == session_id:
            return _SESSION_MATCH_SCORE
        if self.cwd and cwd and _norm(self.cwd) == _norm(cwd):
            return _EXACT_CWD_SCORE
        if self.cwd_prefix and cwd and _path_has_prefix(cwd, self.cwd_prefix):
            return len(_norm(self.cwd_prefix))
        return -1


def _norm(p: str) -> str:
    return Path(p).as_posix().rstrip("/")


def _path_has_prefix(cwd: str, prefix: str) -> bool:
    """Prefix test on path boundaries so ``/a/proj`` does not match prefix ``/a/project``."""
    c, p = _norm(cwd), _norm(prefix)
    return c == p or c.startswith(p + "/")


def _clean_layer3(raw: dict) -> dict:
    """Keep only known Layer-3 keys, coerced to their numeric type and within range."""
    defaults = QAConfig()
    out: dict = {}
    for key, value in (raw or {}).items():
        if key not in QACriteria.EVOLVABLE_KEYS:
            continue
        try:
            coerced = type(getattr(defaults, key))(value)
        except (TypeError, ValueError):
            continue
        if layer3_in_range(key, coerced):
            out[key] = coerced
    return out


def _scope_from_raw(raw, index: int) -> Optional[Scope]:
    """Build one :class:`Scope` from a criteria.yaml / API entry, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    match = raw.get("match")
    if not isinstance(match, dict):
        match = {}
    cwd_prefix = match.get("cwd_prefix")
    cwd = match.get("cwd")
    session_id = match.get("session_id")
    if not cwd_prefix and not cwd and not session_id:
        return None  # a scope with nothing to match on can never apply — drop it
    mode = raw.get("mode")
    if mode not in _MODES:
        mode = None
    domain = []
    for j, d in enumerate(raw.get("add_domain_criteria") or []):
        if not isinstance(d, dict) or not str(d.get("description", "")).strip():
            continue
        item = dict(d)
        item["id"] = str(item.get("id") or f"SC-{index + 1}-{j + 1:03d}")
        domain.append(DomainCriterion.from_dict(item))
    return Scope(
        name=str(raw.get("name") or cwd or cwd_prefix or session_id or f"scope-{index + 1}"),
        cwd_prefix=str(cwd_prefix) if cwd_prefix else None,
        cwd=str(cwd) if cwd else None,
        session_id=str(session_id) if session_id else None,
        mode=mode,
        add_invariants=[str(x) for x in (raw.get("add_invariants") or []) if str(x).strip()],
        add_domain_criteria=domain,
        layer3=_clean_layer3(raw.get("layer3") or {}),
    )


def parse_scopes(raws) -> list[Scope]:
    """Validate a list of raw scope dicts (from the GUI/API), dropping unusable entries."""
    out: list[Scope] = []
    for i, raw in enumerate(raws or []):
        scope = _scope_from_raw(raw, i)
        if scope is not None:
            out.append(scope)
    return out


def scope_to_payload(scope: Scope) -> dict:
    """Plain dict for criteria.yaml / the API. Round-trips through :func:`parse_scopes`."""
    out: dict = {"name": scope.name, "match": {}}
    if scope.cwd:
        out["match"]["cwd"] = scope.cwd
    if scope.cwd_prefix:
        out["match"]["cwd_prefix"] = scope.cwd_prefix
    if scope.session_id:
        out["match"]["session_id"] = scope.session_id
    if scope.mode:
        out["mode"] = scope.mode
    if scope.layer3:
        out["layer3"] = dict(scope.layer3)
    if scope.add_invariants:
        out["add_invariants"] = list(scope.add_invariants)
    if scope.add_domain_criteria:
        out["add_domain_criteria"] = [
            {"id": d.id, "description": d.description, "judge_prompt": d.judge_prompt, "weight": d.weight}
            for d in scope.add_domain_criteria
        ]
    return out


def load_scopes(path: Optional[Path]) -> list[Scope]:
    """Parse the optional ``scopes:`` list from criteria.yaml. Malformed entries are skipped."""
    if path is None or not Path(path).exists():
        return []
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    return parse_scopes(data.get("scopes") or [])


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
