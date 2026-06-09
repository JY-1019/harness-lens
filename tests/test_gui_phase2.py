"""Phase-2 surface: /ui page, /ws node-patch stream, and `show` reading the daemon tree."""

from __future__ import annotations

import argparse

import pytest

from harness_lens.daemon.runtime import DaemonRuntime


def _client(tmp_home):
    from fastapi.testclient import TestClient

    from harness_lens.daemon.app import create_app

    rt = DaemonRuntime(root=tmp_home)
    return TestClient(create_app(runtime=rt)), rt.token


def _session_start(s):
    return {"hook_event_name": "SessionStart", "session_id": s}


def test_ui_served_with_token(tmp_home):
    client, token = _client(tmp_home)
    with client:
        r = client.get("/ui", headers={"host": "127.0.0.1"})
        assert r.status_code == 200
        assert "harness-lens" in r.text and token in r.text  # token embedded for same-origin calls


def test_ws_rejects_bad_token(tmp_home):
    from starlette.websockets import WebSocketDisconnect

    client, _token = _client(tmp_home)
    with client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?token=wrong") as ws:
                ws.receive_json()


def test_ws_streams_patches(tmp_home):
    client, token = _client(tmp_home)
    with client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            hello = ws.receive_json()
            assert hello["op"] == "hello"
            # A hook event must produce an upsert patch on the stream.
            client.post("/hook/claude_code", json=_session_start("WSFLOW"),
                        headers={"X-HL-Token": token})
            patch = ws.receive_json()
            assert patch["op"] == "upsert" and patch["entity"] == "flow"
            assert patch["data"]["flow_id"] == "WSFLOW" and patch["rev"] >= 1


def test_show_reads_daemon_tree(tmp_home, capsys):
    from harness_lens import cli
    from harness_lens.daemon.ledger import DaemonLedger, Flow, Step, Task

    db = tmp_home / "ledger.db"
    ledger = DaemonLedger(db)
    ledger.upsert_flow(Flow(flow_id="F", source="claude_code", status="completed",
                            mode="enforce", title="fix bug"))
    ledger.upsert_task(Task(task_id="t", flow_id="F", kind="turn", title="fix bug", seq=0))
    ledger.upsert_step(Step(step_id="s", task_id="t", flow_id="F", tool_name="Bash",
                            status="ok", tokens=42, seq=0))
    ledger.close()

    assert cli._has_daemon_schema(db) is True
    rc = cli.cmd_show(argparse.Namespace(flow=None, fail=False, limit=20))
    out = capsys.readouterr().out
    assert rc == 0 and "Flow F" in out and "tokens 42" in out and "Bash" in out
    # Single-flow form.
    assert cli.cmd_show(argparse.Namespace(flow="F", fail=False, limit=20)) == 0
    assert "fix bug" in capsys.readouterr().out
