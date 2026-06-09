"""Unit tests for the daemon's pure-logic core: masking, capabilities, adapters, ledger,
migration, policy, approvals."""

from __future__ import annotations

import asyncio

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
    assert CAPABILITIES["codex"]["escalate"] is False
    assert CAPABILITIES["codex"]["update_tool_input"] is False


def test_downgrade_drops_updated_input_for_codex():
    d = Decision.allow(updated_input={"file_path": "x"}, inject_context="ctx")
    out = downgrade(d, "codex")
    assert out.updated_input is None  # codex cannot rewrite tool input
    assert out.inject_context == "ctx"  # codex can inject context
    assert any("updated_input" in note for note in out.downgrades)


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


def test_codex_adapter_escalate_downgrades_to_deny():
    adapter = CodexAdapter()
    ev = adapter.to_event({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash"})
    out = adapter.render(Decision.escalate(layer=2, reason="needs review"), ev)
    assert out["decision"] == "deny"  # codex has no "ask"


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
