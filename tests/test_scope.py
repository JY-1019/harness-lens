"""Project/session-scoped 3-Layer criteria.

A scope lets one project (matched by cwd prefix) or session run under criteria that differ from the
global base — pin a mode, tighten Layer-3, add Layer-1/2 rules. These cover resolution specificity,
path-boundary safety, the pure (non-mutating) overlay, and the end-to-end policy effect the daemon
relies on: a scoped-enforce project denies while the global-observe base allows the same action."""

from __future__ import annotations

from harness_lens.criteria import (
    Scope, ThreeLayerCriteria, apply_scope, load_scopes, resolve_scope,
)
from harness_lens.daemon.capabilities import ALLOW, DENY
from harness_lens.daemon.events import HarnessEvent
from harness_lens.daemon.policy import PolicyEngine

_PROD_DELETE = {"command": 'psql production -c "DELETE FROM users WHERE 1=1"'}


def _ev(cwd: str, session_id: str = "s1") -> HarnessEvent:
    return HarnessEvent(source="claude_code", kind="pre_tool_use", session_id=session_id,
                        tool_name="Bash", tool_input=_PROD_DELETE, cwd=cwd)


# -- resolution ----------------------------------------------------------- #
def test_resolve_longest_prefix_and_session_priority():
    scopes = [Scope("a", cwd_prefix="/p"), Scope("b", cwd_prefix="/p/app"),
              Scope("c", session_id="s9", mode="enforce")]
    assert resolve_scope(scopes, "/p/app/sub", None).name == "b"   # longest matching prefix wins
    assert resolve_scope(scopes, "/p/other", None).name == "a"
    assert resolve_scope(scopes, "/q", None) is None               # no match → global base
    assert resolve_scope(scopes, "/p/app/sub", "s9").name == "c"   # exact session beats any path


def test_prefix_respects_path_boundaries():
    scopes = [Scope("x", cwd_prefix="/a/project")]
    assert resolve_scope(scopes, "/a/project", None).name == "x"
    assert resolve_scope(scopes, "/a/project/pkg", None).name == "x"
    assert resolve_scope(scopes, "/a/proj", None) is None          # not on a path boundary
    assert resolve_scope(scopes, "/a/projectile", None) is None


# -- overlay (pure) ------------------------------------------------------- #
def test_apply_scope_overlays_without_mutating_base():
    base = ThreeLayerCriteria.load(None)
    base_invariants = len(base.invariants)
    scope = Scope("x", cwd_prefix="/p", add_invariants=["추가 규칙"],
                  layer3={"retry_threshold": 1, "quality_threshold": 0.95})
    eff = apply_scope(base, scope)

    assert "추가 규칙" in eff.invariants
    assert eff.qa.effective("retry_threshold") == 1
    assert eff.qa.effective("quality_threshold") == 0.95
    # an unset key keeps the base value
    assert eff.qa.effective("latency_multiplier") == base.qa.effective("latency_multiplier")
    # base is untouched
    assert len(base.invariants) == base_invariants
    assert base.qa.effective("retry_threshold") == 3


def test_apply_scope_none_returns_base():
    base = ThreeLayerCriteria.load(None)
    assert apply_scope(base, None) is base


def test_apply_scope_ignores_unknown_layer3_key():
    base = ThreeLayerCriteria.load(None)
    eff = apply_scope(base, Scope("x", cwd_prefix="/p", layer3={"bogus": 9}))
    assert eff.qa.effective("retry_threshold") == base.qa.effective("retry_threshold")


# -- loading -------------------------------------------------------------- #
def test_load_scopes_parses_and_skips_invalid(tmp_path):
    (tmp_path / "criteria.yaml").write_text(
        """
invariants: ["프로덕션 DB에 직접 DELETE를 실행하지 않는다"]
scopes:
  - name: payments
    match: { cwd_prefix: "/work/pay" }
    mode: enforce
    layer3: { retry_threshold: 1 }
    add_invariants: ["추가"]
  - name: bogus-mode
    match: { session_id: "abc" }
    mode: not-a-mode
  - name: no-match
    mode: enforce
""",
        encoding="utf-8",
    )
    scopes = load_scopes(tmp_path / "criteria.yaml")
    names = [s.name for s in scopes]
    assert "payments" in names and "no-match" not in names  # match-less scope dropped
    pay = next(s for s in scopes if s.name == "payments")
    assert pay.cwd_prefix == "/work/pay" and pay.mode == "enforce"
    assert pay.layer3 == {"retry_threshold": 1} and pay.add_invariants == ["추가"]
    bogus = next(s for s in scopes if s.name == "bogus-mode")
    assert bogus.mode is None  # invalid mode coerced to inherit-global


def test_load_scopes_absent_or_missing_file(tmp_path):
    assert load_scopes(None) == []
    assert load_scopes(tmp_path / "nope.yaml") == []
    (tmp_path / "c.yaml").write_text("invariants: []\n", encoding="utf-8")
    assert load_scopes(tmp_path / "c.yaml") == []  # no scopes section


# -- end-to-end policy effect (what the daemon does) ---------------------- #
def test_scoped_enforce_denies_while_global_observes():
    base = ThreeLayerCriteria.load(None)
    scopes = [Scope("secure", cwd_prefix="/p/secure", mode="enforce")]
    global_mode = "observe"

    # A session inside the scoped project: resolve → enforce → deny.
    ev = _ev("/p/secure/app")
    scope = resolve_scope(scopes, ev.cwd, ev.session_id)
    assert scope is not None
    eng = PolicyEngine(apply_scope(base, scope))
    assert eng.evaluate_pre_tool(ev, scope.mode or global_mode).action == DENY

    # A session elsewhere: no scope → global observe → allowed (recorded, not blocked).
    other = _ev("/p/other", session_id="s2")
    assert resolve_scope(scopes, other.cwd, other.session_id) is None
    assert PolicyEngine(apply_scope(base, None)).evaluate_pre_tool(other, global_mode).action == ALLOW
