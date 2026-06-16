"""End-to-end tests through DaemonRuntime.handle and the FastAPI app (TestClient)."""

from __future__ import annotations

import asyncio

from harness_lens.daemon.runtime import DaemonRuntime


def _pre_tool(session, tool, tool_input, tool_use_id="t1"):
    return {
        "hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool,
        "tool_input": tool_input, "tool_use_id": tool_use_id,
    }


def _session_start(session):
    return {"hook_event_name": "SessionStart", "session_id": session}


def _user_prompt(session, text):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}


def _post_tool(session, tool, ok=True, tool_use_id="t1"):
    name = "PostToolUse" if ok else "PostToolUseFailure"
    return {"hook_event_name": name, "session_id": session, "tool_name": tool,
            "tool_use_id": tool_use_id, "tool_response": {"ok": ok} if ok else {"error": "boom"}}


def test_observe_records_without_blocking(tmp_home):
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        await rt.handle("claude_code", _session_start("S"))
        await rt.handle("claude_code", _user_prompt("S", "fix the bug"))
        resp = await rt.handle("claude_code", _pre_tool("S", "Bash", {"command": "DELETE FROM t -- production"}))
        await rt.handle("claude_code", _post_tool("S", "Bash"))
        tree = rt.ledger.flow_tree("S")
        await rt.stop()
        return resp, tree

    resp, tree = asyncio.run(go())
    # observe mode: even an L1-matching command is allowed (empty/allow response), only recorded.
    assert resp.get("hookSpecificOutput", {}).get("permissionDecision") in ("allow", None)
    assert tree["title"].startswith("fix the bug")
    step = tree["tasks"][0]["steps"][0]
    assert step["tool_name"] == "Bash" and step["status"] == "ok"


def test_enforce_l1_deny(tmp_home):
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        rt.set_mode("enforce")
        await rt.handle("claude_code", _session_start("S"))
        resp = await rt.handle("claude_code", _pre_tool("S", "Bash", {"command": "DELETE FROM users -- production"}))
        step = rt.ledger.flow_tree("S")["tasks"][0]["steps"][0]
        await rt.stop()
        return resp, step

    resp, step = asyncio.run(go())
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert step["status"] == "denied" and step["decision_layer"] == 1


def test_enforce_escalate_then_approved(tmp_home):
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        rt.set_mode("enforce")
        rt.config.approval_timeout_sec = 5.0
        await rt.handle("claude_code", _session_start("S"))
        # Edit without a prior Read → L2 escalate → parks an approval.
        handle = asyncio.create_task(
            rt.handle("claude_code", _pre_tool("S", "Edit", {"file_path": "/repo/x.py"}))
        )
        # Wait for the approval to register, then approve it.
        for _ in range(100):
            await asyncio.sleep(0.01)
            pending = rt.ledger.pending_approvals()
            if pending:
                break
        assert pending, "escalation never registered an approval"
        rt.resolve_approval(pending[0].approval_id, "approved")
        resp = await handle
        await rt.stop()
        return resp

    resp = asyncio.run(go())
    assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_enforce_escalate_timeout_denies(tmp_home):
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        rt.set_mode("enforce")
        rt.config.approval_timeout_sec = 0.1  # no human answers in time
        rt.config.default_on_timeout = "deny"
        await rt.handle("claude_code", _session_start("S"))
        resp = await rt.handle("claude_code", _pre_tool("S", "Edit", {"file_path": "/repo/y.py"}))
        await rt.stop()
        return resp

    resp = asyncio.run(go())
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_two_concurrent_sessions_distinct_flows(tmp_home):
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        await rt.handle("claude_code", _session_start("A"))
        await rt.handle("codex", _session_start("B"))
        await rt.handle("claude_code", _pre_tool("A", "Bash", {"command": "ls"}, "ta"))
        await rt.handle("codex", _pre_tool("B", "Bash", {"command": "pwd"}, "tb"))
        flows = {f.flow_id: f.source for f in rt.ledger.list_flows()}
        await rt.stop()
        return flows

    flows = asyncio.run(go())
    assert flows == {"A": "claude_code", "B": "codex"}


def test_restart_resumes_open_turn_without_titleless_turn(tmp_home):
    """A daemon restart mid-session must not spawn a titleless "(요청 미관측)" turn: the next
    tool resumes the Flow's open turn (rehydrated from the ledger), keeping its prompt title."""
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        await rt.handle("claude_code", _session_start("S"))
        await rt.handle("claude_code", _user_prompt("S", "do the thing"))
        await rt.handle("claude_code", _pre_tool("S", "Bash", {"command": "ls"}, "t1"))
        await rt.stop()
        # Simulate restart: a brand-new runtime over the same ledger, with NO session_start /
        # user_prompt replay — exactly what an in-progress session looks like after a restart.
        rt2 = DaemonRuntime(root=tmp_home)
        await rt2.start()
        await rt2.handle("claude_code", _pre_tool("S", "Read", {"file_path": "x"}, "t2"))
        tree = rt2.ledger.flow_tree("S")
        await rt2.stop()
        return tree

    tree = asyncio.run(go())
    turns = [t for t in tree["tasks"] if t["kind"] == "turn"]
    assert len(turns) == 1                      # resumed, not a second titleless turn
    assert turns[0]["title"] == "do the thing"  # original prompt preserved
    assert len(turns[0]["steps"]) == 2          # both tools landed on the one turn


def test_conditional_l2_criterion_does_not_block_control_path(tmp_home):
    """Branching harness sanity: a project scope (exact cwd) with a *conditional* natural-language
    L2 criterion is applied to the effective harness, and the control path keeps working — such
    criteria are scored by the async Judge, not gated in real time (only DC-001 gates edits)."""
    async def go():
        rt = DaemonRuntime(root=tmp_home)
        await rt.start()
        rt.save_scopes([{
            "name": "pay", "match": {"cwd": "/repo/pay"}, "mode": "enforce",
            "add_domain_criteria": [{"id": "X1", "description": "결제 코드 수정 시 docs 참고", "weight": 1}],
        }])
        ev = lambda **k: {"session_id": "P", "cwd": "/repo/pay", **k}
        await rt.handle("claude_code", ev(hook_event_name="SessionStart"))
        await rt.handle("claude_code", ev(hook_event_name="PreToolUse", tool_name="Read",
                                          tool_input={"file_path": "/repo/pay/a.py"}, tool_use_id="r"))
        resp = await rt.handle("claude_code", ev(hook_event_name="PreToolUse", tool_name="Edit",
                                                 tool_input={"file_path": "/repo/pay/a.py"}, tool_use_id="e"))
        eff = rt.effective_criteria_payload(cwd="/repo/pay", session_id="P")
        await rt.stop()
        return resp, eff

    resp, eff = asyncio.run(go())
    assert resp["hookSpecificOutput"]["permissionDecision"] == "allow"   # read-before-edit ok
    assert eff["mode"] == "enforce"                                       # branch's mode applied
    assert any(dc["id"] == "X1" for dc in eff["domain_criteria"])         # conditional criterion in force


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
def _client(tmp_home):
    from fastapi.testclient import TestClient

    from harness_lens.daemon.app import create_app

    rt = DaemonRuntime(root=tmp_home)
    return TestClient(create_app(runtime=rt)), rt.token


def test_app_requires_token(tmp_home):
    client, _token = _client(tmp_home)
    with client:
        assert client.get("/api/status").status_code == 403


def test_app_status_and_mode(tmp_home):
    client, token = _client(tmp_home)
    h = {"X-HL-Token": token}
    with client:
        assert client.get("/api/status", headers=h).json()["mode"] == "observe"
        assert client.post("/api/mode", json={"mode": "enforce"}, headers=h).json()["mode"] == "enforce"
        assert client.get("/api/status", headers=h).json()["mode"] == "enforce"
        assert client.post("/api/mode", json={"mode": "bogus"}, headers=h).status_code == 400


def test_app_hook_and_tree(tmp_home):
    client, token = _client(tmp_home)
    h = {"X-HL-Token": token}
    with client:
        client.post("/hook/claude_code", json=_session_start("S"), headers=h)
        client.post("/hook/claude_code", json=_user_prompt("S", "do a thing"), headers=h)
        client.post("/hook/claude_code", json=_pre_tool("S", "Bash", {"command": "ls"}), headers=h)
        client.post("/hook/claude_code", json=_post_tool("S", "Bash"), headers=h)
        flows = client.get("/api/flows", headers=h).json()
        assert any(f["flow_id"] == "S" for f in flows)
        tree = client.get("/api/flows/S/tree", headers=h).json()
        assert tree["tasks"][0]["steps"][0]["tool_name"] == "Bash"


def test_app_enforce_timeout_deny_via_http(tmp_home):
    client, token = _client(tmp_home)
    h = {"X-HL-Token": token}
    with client:
        client.app.state.runtime.set_mode("enforce")
        client.app.state.runtime.config.approval_timeout_sec = 0.1
        client.post("/hook/claude_code", json=_session_start("S"), headers=h)
        resp = client.post(
            "/hook/claude_code", json=_pre_tool("S", "Edit", {"file_path": "/repo/z.py"}), headers=h
        ).json()
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"
