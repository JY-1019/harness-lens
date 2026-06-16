"""Unit tests for the daemon's pure-logic core: masking, capabilities, adapters, ledger,
migration, policy, approvals."""

from __future__ import annotations

import asyncio
import json

from harness_lens.daemon.adapters import get_adapter
from harness_lens.daemon.adapters.claude_code import ClaudeCodeAdapter
from harness_lens.daemon.adapters.codex import CodexAdapter
from harness_lens.daemon.approvals import APPROVED, ApprovalQueue
from harness_lens.daemon.capabilities import CAPABILITIES, Decision, downgrade
from harness_lens.daemon.events import REDACTION_PLACEHOLDER, mask_secrets
from harness_lens.daemon.ledger import (
    DaemonLedger,
    Flow,
    Step,
    Task,
    migrate_legacy,
    relocate_legacy_db,
)
from harness_lens.daemon.policy import PolicyContext, PolicyEngine
from harness_lens.criteria import ThreeLayerCriteria


# --------------------------------------------------------------------------- #
# Secret masking
# --------------------------------------------------------------------------- #
def test_mask_secrets_by_key_and_shape():
    raw = {
        "api_key": "should-vanish",
        "nested": {"password": "hunter2", "ok": "keep"},
        "blob": "token sk-ABCDEFGH12345678 here",
        "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
        "list": ["AKIAABCDEFGH1234567", "plain"],
    }
    masked = mask_secrets(raw)
    assert masked["api_key"] == REDACTION_PLACEHOLDER
    assert masked["nested"]["password"] == REDACTION_PLACEHOLDER
    assert masked["nested"]["ok"] == "keep"
    assert REDACTION_PLACEHOLDER in masked["blob"] and "sk-ABCDEFGH" not in masked["blob"]
    assert REDACTION_PLACEHOLDER in masked["pem"] and "MIIB" not in masked["pem"]
    assert masked["list"][0] == REDACTION_PLACEHOLDER and masked["list"][1] == "plain"
    # Purity: the original is untouched.
    assert raw["api_key"] == "should-vanish"


# --------------------------------------------------------------------------- #
# Capability matrix + downgrade
# --------------------------------------------------------------------------- #
def test_capability_matrix_shape():
    assert CAPABILITIES["claude_code"]["escalate"] is True
    # Codex's runtime validator has no "ask" and rejects updatedInput → those caps are False.
    assert CAPABILITIES["codex"]["escalate"] is False
    assert CAPABILITIES["codex"]["update_tool_input"] is False
    assert CAPABILITIES["codex"]["inject_context"] is True


def test_downgrade_drops_updated_input_for_codex():
    # Codex cannot rewrite tool input (updatedInput needs the unsupported permissionDecision:allow);
    # additionalContext is fine.
    d = Decision.allow(updated_input={"file_path": "x"}, inject_context="ctx")
    out = downgrade(d, "codex")
    assert out.updated_input is None
    assert out.inject_context == "ctx"
    assert any("updated_input" in n for n in out.downgrades)


def test_downgrade_noop_for_claude():
    d = Decision.allow(updated_input={"a": 1})
    out = downgrade(d, "claude_code")
    assert out.updated_input == {"a": 1}
    assert out.downgrades == []


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #
def test_claude_adapter_to_event_pre_tool():
    ev = ClaudeCodeAdapter().to_event({
        "hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
        "tool_input": {"command": "ls"}, "tool_use_id": "t1", "cwd": "/repo",
    })
    assert ev is not None
    assert ev.kind == "pre_tool_use" and ev.source == "claude_code"
    assert ev.tool_name == "Bash" and ev.tool_use_id == "t1" and ev.is_control


def test_claude_adapter_renders_deny():
    adapter = ClaudeCodeAdapter()
    ev = adapter.to_event({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash"})
    out = adapter.render(Decision.deny(layer=1, reason="nope"), ev)
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny" and spec["permissionDecisionReason"] == "nope"


def test_codex_adapter_renders_pre_tool_schema():
    # Codex's runtime validator only honours permissionDecision "deny" (with a non-empty reason).
    # It rejects "allow"/"ask" → allow renders as empty output; escalate collapses to deny.
    adapter = CodexAdapter()
    ev = adapter.to_event({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash"})

    deny = adapter.render(Decision.deny(layer=1, reason="nope"), ev)
    assert "decision" not in deny
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert deny["hookSpecificOutput"]["permissionDecisionReason"] == "nope"

    # deny with an empty reason still gets a non-empty reason (Codex requires it).
    deny2 = adapter.render(Decision.deny(layer=1, reason=""), ev)
    assert deny2["hookSpecificOutput"]["permissionDecisionReason"]

    # escalate has no "ask" on Codex → collapses to deny (with a reason).
    esc = adapter.render(Decision.escalate(layer=2, reason="needs review"), ev)
    assert esc["hookSpecificOutput"]["permissionDecision"] == "deny"

    # allow → empty output (Codex rejects permissionDecision:allow); updatedInput is dropped.
    allow = adapter.render(Decision.allow(updated_input={"command": "ls -a"}), ev)
    assert allow == {}


def test_codex_adapter_user_prompt_allow_is_noop():
    adapter = CodexAdapter()
    ev = adapter.to_event({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"})
    assert adapter.render(Decision.allow(), ev) == {}


def test_claude_adapter_user_prompt_not_pretooluse():
    # A UserPromptSubmit must not return a PreToolUse hookSpecificOutput (Claude rejects the mismatch).
    adapter = ClaudeCodeAdapter()
    ev = adapter.to_event({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": "hi"})
    assert adapter.render(Decision.allow(), ev) == {}
    inj = adapter.render(Decision.allow(inject_context="note"), ev)
    assert inj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_codex_adapter_stop_blocks_with_top_level_decision():
    adapter = CodexAdapter()
    ev = adapter.to_event({"hook_event_name": "Stop", "session_id": "s"})
    out = adapter.render(Decision.deny(layer=3, reason="not done"), ev)
    assert out == {"decision": "block", "reason": "not done"}
    assert adapter.render(Decision.allow(), ev) == {}


def test_unknown_event_is_ignored():
    assert ClaudeCodeAdapter().to_event({"hook_event_name": "Nonsense"}) is None
    assert get_adapter("nope") is None


# --------------------------------------------------------------------------- #
# Ledger + tree + migration
# --------------------------------------------------------------------------- #
def test_ledger_tree_roundtrip(tmp_home):
    ledger = DaemonLedger(tmp_home / "ledger.db")
    ledger.upsert_flow(Flow(flow_id="f1", source="claude_code", status="running", mode="observe"))
    ledger.upsert_task(Task(task_id="t1", flow_id="f1", kind="turn", seq=0))
    ledger.upsert_task(Task(task_id="t2", flow_id="f1", kind="subagent", parent_task_id="t1", seq=1))
    ledger.upsert_step(Step(step_id="s1", task_id="t1", flow_id="f1", tool_name="Bash",
                            status="ok", tokens=10, seq=0))
    ledger.upsert_step(Step(step_id="s2", task_id="t2", flow_id="f1", tool_name="Read",
                            status="ok", tokens=5, seq=0))
    tree = ledger.flow_tree("f1")
    assert tree["total_tokens"] == 15  # accrued from steps
    roots = tree["tasks"]
    assert len(roots) == 1 and roots[0]["task_id"] == "t1"
    assert roots[0]["children"][0]["task_id"] == "t2"  # subagent nested under its parent
    ledger.close()


def test_list_flows_has_cwd_filter(tmp_home):
    ledger = DaemonLedger(tmp_home / "ledger.db")
    ledger.upsert_flow(Flow(flow_id="proj", source="codex", cwd="/repo/app"))
    ledger.upsert_flow(Flow(flow_id="nocwd", source="claude_code", cwd=None))
    ledger.upsert_flow(Flow(flow_id="blank", source="claude_code", cwd=""))
    # Default lists every flow; has_cwd drops the cwd-less subagent/tool sessions.
    assert {f.flow_id for f in ledger.list_flows()} == {"proj", "nocwd", "blank"}
    assert [f.flow_id for f in ledger.list_flows(has_cwd=True)] == ["proj"]
    ledger.close()


def test_usage_for_step_attributes_service_harness():
    from harness_lens.daemon.runtime import DaemonRuntime
    u = DaemonRuntime.usage_for_step
    # A shell command is attributed to the scaffolding it touches, even though the tool is "Bash" —
    # this is what makes Codex's wall of shell steps legible.
    assert {x["kind"] for x in u("Bash", '{"command":"cat .claude/skills/foo/SKILL.md"}', "")} == {"skill"}
    assert any(x["kind"] == "instruction" and x["name"] == "CLAUDE.md"
               for x in u("Bash", '{"command":"sed -n 1,3p CLAUDE.md"}', ""))
    assert u("mcp__node_repl__js", "{}", "")[0]["kind"] == "mcp"
    assert u("Bash", '{"command":"ls -la"}', "") == []  # plain shell → no scaffolding attributed


def test_service_harness_payload_includes_cursor_rules(tmp_home):
    from harness_lens.daemon.runtime import DaemonRuntime
    rt = DaemonRuntime(root=tmp_home)
    rt.ledger.upsert_flow(Flow(flow_id="F", source="codex", cwd=str(tmp_home)))
    rules = tmp_home / ".cursor" / "rules"; rules.mkdir(parents=True)
    (rules / "style.mdc").write_text("be terse", encoding="utf-8")
    sh = rt.service_harness_payload("F")
    assert sh["flow_id"] == "F" and sh["source"] == "codex"
    # .cursor/rules is in the audit scope even though Cursor is not a tracked runtime.
    assert any(c["component"] == "cursor_rules" and c["scope"] == "프로젝트" for c in sh["components"])
    assert rt.service_harness_payload("nope") is None


def test_relay_fail_closed_codex_uses_new_schema(tmp_home, capsys):
    # On a fail-closed outage, Codex must get the new hookSpecificOutput shape — the legacy
    # {"decision":"deny"} is rejected by Codex ≥0.139 as "invalid pre-tool-use JSON output".
    from harness_lens.daemon import client
    from harness_lens.daemon.config import DaemonConfig

    cfg = DaemonConfig.load(tmp_home); cfg.fail_open = False
    rc = client._on_outage("codex", {"hook_event_name": "PreToolUse"}, cfg, True,
                           Exception("down"), tmp_home)
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "decision" not in data  # NOT the legacy top-level decision
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_component_prompt_resolves_scaffolding(tmp_home, tmp_path):
    from harness_lens.daemon.runtime import DaemonRuntime
    rt = DaemonRuntime(root=tmp_home)
    proj = tmp_path / "proj"
    (proj / ".claude" / "skills" / "demo").mkdir(parents=True)
    (proj / ".claude" / "skills" / "demo" / "SKILL.md").write_text("# demo skill\nuse me", encoding="utf-8")
    (proj / ".cursor" / "rules").mkdir(parents=True)
    (proj / ".cursor" / "rules" / "style.mdc").write_text("be terse", encoding="utf-8")
    (proj / "AGENTS.md").write_text("project agents guide", encoding="utf-8")

    sk = rt.component_prompt("skill", "demo", str(proj))
    assert sk["found"] and "demo skill" in sk["content"]
    cr = rt.component_prompt("cursor_rule", "style", str(proj))
    assert cr["found"] and "be terse" in cr["content"]
    ins = rt.component_prompt("instruction", "AGENTS.md", str(proj))
    assert ins["found"] and "project agents" in ins["content"]
    assert rt.component_prompt("skill", "nope", str(proj))["found"] is False


def test_large_harness_scope_loads_and_persists(tmp_home):
    # A deliberately huge per-project harness (dozens of L2 + many L1) must load, apply to the
    # effective harness, and survive a reload — no silent truncation.
    from harness_lens.daemon.runtime import DaemonRuntime
    rt = DaemonRuntime(root=tmp_home)
    crit = [{"id": f"BIZ-{i:03d}", "description": f"business rule {i}", "weight": 1 + (i % 3)} for i in range(60)]
    invs = [f"governance rule {i}" for i in range(12)]
    rt.save_scopes([{"name": "big", "match": {"cwd": "/repo/big"}, "mode": "enforce",
                     "add_invariants": invs, "add_domain_criteria": crit, "layer3": {"quality_threshold": 0.95}}])
    eff = rt.effective_criteria_payload(cwd="/repo/big", session_id="S")
    assert eff["mode"] == "enforce"
    assert len(eff["domain_criteria"]) >= 60 and len(eff["invariants"]) >= 12
    rt.reload_criteria()  # round-trips through criteria.yaml on disk
    eff2 = rt.effective_criteria_payload(cwd="/repo/big", session_id="S")
    assert len(eff2["domain_criteria"]) >= 60 and len(eff2["invariants"]) >= 12


def test_l3_status_flags_threshold_breaches():
    from harness_lens.daemon.runtime import _l3_status
    tasks = [{"steps": [{"status": "failed"}, {"status": "failed"}, {"status": "ok"}], "children": []}]
    st = _l3_status(tasks, {"failure_count_trigger": 2, "latency_multiplier": 3.0, "quality_threshold": 0.85})
    assert st["failures"]["value"] == 2 and st["failures"]["breached"] is True
    assert st["breached"] is True
    ok = _l3_status([{"steps": [{"status": "ok"}], "children": []}], {"failure_count_trigger": 2})
    assert ok["breached"] is False


def test_l1_detector_reason_carries_evidence():
    # The L1 deny reason names the rule AND the matched evidence (the offending fragment).
    from harness_lens.criteria.invariant import InvariantChecker
    from types import SimpleNamespace
    step = SimpleNamespace(tool_name="Bash", input_summary='psql prod -c "DELETE FROM ledger"', output_summary="")
    passed, viols = InvariantChecker(["프로덕션 DB에 직접 DELETE를 실행하지 않는다"]).check(step)
    assert not passed and "프로덕션 표식" in viols[0].detail  # evidence, not a generic phrase


def test_pending_approval_denied_on_restart(tmp_home):
    from harness_lens.daemon.ledger import Approval

    ledger = DaemonLedger(tmp_home / "ledger.db")
    ledger.upsert_flow(Flow(flow_id="f", source="codex", status="running", mode="enforce"))
    ledger.upsert_task(Task(task_id="t", flow_id="f", kind="turn", seq=0))
    ledger.upsert_step(Step(step_id="s", task_id="t", flow_id="f", tool_name="Bash",
                            status="pending_approval", seq=0))
    ledger.add_approval(Approval(approval_id="a", step_id="s"))
    n = ledger.deny_all_pending("daemon restart")
    assert n == 1
    assert ledger.pending_approvals() == []
    assert ledger.get_step("s").status == "denied"
    ledger.close()


def test_legacy_migration(tmp_home):
    # Build a legacy observe-only ledger via the old SQLiteStore.
    from harness_lens.store import Session, SQLiteStore, Step as LStep

    legacy = SQLiteStore(tmp_home / "ledger.db")
    legacy.upsert_session(Session(session_id="sess1", platform="claude-code", started_at=1.0,
                                  status="completed", total_tokens=99))
    legacy.add_step(LStep(session_id="sess1", flow_id="flowX", task_id="taskA",
                          task_category="실행", tool_name="Bash", input_summary="run tests",
                          success=True, timestamp=1.0))
    legacy.close()

    # Daemon takes over: relocate the legacy DB and import it.
    relocate_legacy_db(tmp_home)
    assert (tmp_home / "ledger.legacy.db").exists()
    ledger = DaemonLedger(tmp_home / "ledger.db")
    summary = migrate_legacy(ledger)
    assert summary["flows"] == 1 and summary["steps"] == 1
    flow = ledger.get_flow("sess1")
    assert flow is not None and flow.source == "claude_code" and flow.status == "completed"
    tree = ledger.flow_tree("sess1")
    assert tree["tasks"][0]["steps"][0]["tool_name"] == "Bash"
    # Idempotent: a second run imports nothing new.
    assert migrate_legacy(ledger)["flows"] == 0
    ledger.close()


# --------------------------------------------------------------------------- #
# Policy engine
# --------------------------------------------------------------------------- #
def _criteria():
    return ThreeLayerCriteria.load(None)  # falls back to DEFAULT_CRITERIA_YAML


def _pre_tool_event(tool_name, tool_input):
    from harness_lens.daemon.events import HarnessEvent

    return HarnessEvent(source="claude_code", kind="pre_tool_use", session_id="s",
                        tool_name=tool_name, tool_input=tool_input)


def test_policy_l1_denies_prod_delete_in_enforce():
    pol = PolicyEngine(_criteria())
    ev = _pre_tool_event("Bash", {"command": "DELETE FROM users; -- production cleanup"})
    d = pol.evaluate_pre_tool(ev, "enforce")
    assert d.action == "deny" and d.layer == 1


def test_policy_l1_records_but_allows_in_observe():
    pol = PolicyEngine(_criteria())
    ev = _pre_tool_event("Bash", {"command": "DELETE FROM users; -- production cleanup"})
    d = pol.evaluate_pre_tool(ev, "observe")
    assert d.action == "allow" and d.layer == 1  # recorded, not blocked


def test_policy_l2_escalates_edit_without_read():
    pol = PolicyEngine(_criteria())
    ev = _pre_tool_event("Edit", {"file_path": "/repo/app.py"})
    d = pol.evaluate_pre_tool(ev, "enforce")
    assert d.action == "escalate" and d.layer == 2


def test_policy_l2_allows_edit_after_read():
    pol = PolicyEngine(_criteria())
    ev = _pre_tool_event("Edit", {"file_path": "/repo/app.py"})
    ctx = PolicyContext(read_paths={"/repo/app.py"})
    assert pol.evaluate_pre_tool(ev, "enforce", ctx).action == "allow"


def _criteria_with_l2(*descriptions):
    from harness_lens.criteria.domain import DomainCriterion
    base = ThreeLayerCriteria.load(None)
    base.domain_criteria = [DomainCriterion.from_dict({"id": f"BIZ-{i}", "description": d, "weight": 1})
                            for i, d in enumerate(descriptions)]
    return base


def test_policy_l2_structural_test_before_change():
    # A "회귀 테스트 동반" criterion → committing/deploying without a prior test escalates …
    pol = PolicyEngine(_criteria_with_l2("배포·정산 로직 변경 시 회귀 테스트를 동반한다"))
    commit = _pre_tool_event("Bash", {"command": "git commit -m 'ship payment change'"})
    d = pol.evaluate_pre_tool(commit, "enforce", PolicyContext(ran_tests=False))
    assert d.action == "escalate" and d.layer == 2 and "BIZ-0" in d.reason
    # … but once tests have run in the flow, the same commit is allowed.
    assert pol.evaluate_pre_tool(commit, "enforce", PolicyContext(ran_tests=True)).action == "allow"


def test_policy_l3_circuit_breaker_escalates_after_failures():
    # A project L3 failure_count_trigger acts as a circuit breaker: once the flow's accumulated
    # failures cross it, the next action escalates (enforce only).
    crit = ThreeLayerCriteria.load(None)
    crit.qa.set_override("failure_count_trigger", 2)
    pol = PolicyEngine(crit)
    ev = _pre_tool_event("Bash", {"command": "echo continue"})
    assert pol.evaluate_pre_tool(ev, "enforce", PolicyContext(failed_steps=1)).action == "allow"
    d = pol.evaluate_pre_tool(ev, "enforce", PolicyContext(failed_steps=2))
    assert d.action == "escalate" and d.layer == 3 and "L3" in d.reason
    assert pol.evaluate_pre_tool(ev, "observe", PolicyContext(failed_steps=5)).action == "allow"  # observe never gates


def test_policy_l2_structural_no_skip_verify():
    pol = PolicyEngine(_criteria_with_l2("CI 검증을 우회하지 않는다"))
    ev = _pre_tool_event("Bash", {"command": "git commit --no-verify -m wip"})
    assert pol.evaluate_pre_tool(ev, "enforce", PolicyContext(ran_tests=True)).action == "escalate"
    # a value-level criterion with no structural detector stays advisory (no real-time gate)
    pol2 = PolicyEngine(_criteria_with_l2("환불 금액은 원 결제 금액을 초과할 수 없다"))
    assert pol2.evaluate_pre_tool(ev, "enforce", PolicyContext(ran_tests=True)).action == "allow"


def test_policy_l2_structural_payment_velocity():
    # "동일 카드로 단시간 다발 결제는 속도 제한을 적용한다" → editing charge code with no velocity/
    # rate-limit guard escalates; the same edit WITH a guard is allowed.
    pol = PolicyEngine(_criteria_with_l2("동일 카드로 단시간 다발 결제는 속도 제한을 적용한다"))
    ctx = PolicyContext(read_paths={"/repo/charge.py"})  # pre-read so DC-001 doesn't fire first
    no_guard = _pre_tool_event("Edit", {"file_path": "/repo/charge.py",
        "new_string": "def charge_card(card, amount):\n    return gateway.charge(card, amount)"})
    d = pol.evaluate_pre_tool(no_guard, "enforce", ctx)
    assert d.action == "escalate" and d.layer == 2 and d.criterion_id == "BIZ-0"
    assert "속도 제한" in d.reason
    with_guard = _pre_tool_event("Edit", {"file_path": "/repo/charge.py",
        "new_string": "def charge_card(card, amount):\n    rate_limit(card)\n    return gateway.charge(card, amount)"})
    assert pol.evaluate_pre_tool(with_guard, "enforce", ctx).action == "allow"
    # Honest boundary: a non-edit step (the runtime behaviour) is not gated — only authored code is.
    assert pol.evaluate_pre_tool(_pre_tool_event("Bash", {"command": "echo charge"}), "enforce", PolicyContext()).action == "allow"


def test_policy_decisions_pinpoint_which_rule_fired():
    """Each gating decision carries criterion_id + names the rule, so the GUI can highlight the
    specific constraint among many (L1 invariant / L2 criterion id / L3 threshold key)."""
    # L2: the fired criterion is identified by id AND its wording appears in the reason.
    pol = PolicyEngine(_criteria_with_l2(
        "주문 합계는 항상 라인아이템 합과 일치해야 한다",            # BIZ-0 (no detector)
        "배포·정산 변경 시 회귀 테스트를 동반한다",                 # BIZ-1 (test-before-change)
    ))
    commit = _pre_tool_event("Bash", {"command": "git commit -m ship"})
    d = pol.evaluate_pre_tool(commit, "enforce", PolicyContext(ran_tests=False))
    assert d.action == "escalate" and d.layer == 2
    assert d.criterion_id == "BIZ-1"                       # exactly which of the rules fired
    assert "회귀 테스트" in d.reason                        # its wording, not just an id

    # L1: deny carries the invariant text it tripped.
    pol2 = PolicyEngine(_criteria())
    den = pol2.evaluate_pre_tool(_pre_tool_event("Bash", {"command": "DELETE FROM users; -- production"}), "enforce")
    assert den.action == "deny" and den.layer == 1 and den.criterion_id

    # L3: circuit breaker tags the threshold key.
    crit = ThreeLayerCriteria.load(None); crit.qa.set_override("failure_count_trigger", 2)
    l3 = PolicyEngine(crit).evaluate_pre_tool(
        _pre_tool_event("Bash", {"command": "echo go"}), "enforce", PolicyContext(failed_steps=2))
    assert l3.action == "escalate" and l3.criterion_id == "failure_count_trigger"


# --------------------------------------------------------------------------- #
# Approval queue
# --------------------------------------------------------------------------- #
def test_approval_queue_resolve():
    async def go():
        q = ApprovalQueue()
        q.register("a1")
        task = asyncio.create_task(q.wait("a1", timeout=5))
        await asyncio.sleep(0.01)
        assert q.resolve("a1", APPROVED) is True
        return await task

    assert asyncio.run(go()) == APPROVED


def test_approval_queue_timeout():
    async def go():
        q = ApprovalQueue()
        q.register("a2")
        return await q.wait("a2", timeout=0.05)

    assert asyncio.run(go()) == "timeout"
