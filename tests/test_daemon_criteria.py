"""The live daemon GUI can view and edit the base 3-Layer harness.

The daemon (not the legacy gui.py) is what ``harness-lens gui`` shows while it is running, so the
human-owner harness editor lives here too: ``GET /api/criteria`` reads the base layers and
``POST /api/criteria/{layer}`` edits one, hot-reloading the live policy. AHE auto-evolution is
still pinned to Layer 3 by the CriteriaGuard — these routes are the human's explicit edit path.
"""

from __future__ import annotations

import pytest

from harness_lens.daemon.ledger import Flow
from harness_lens.daemon.runtime import DaemonRuntime


@pytest.fixture
def no_enforce(monkeypatch):
    """Keep criteria edits from re-enforcing the developer's real CLAUDE.md / AGENTS.md."""
    import harness_lens.detector as detector

    monkeypatch.setattr(detector, "detect_all", lambda: [])


def _client(tmp_home):
    from fastapi.testclient import TestClient

    from harness_lens.daemon.app import create_app

    rt = DaemonRuntime(root=tmp_home)
    return TestClient(create_app(runtime=rt)), rt


def test_get_criteria_shape(tmp_home):
    client, rt = _client(tmp_home)
    with client:
        r = client.get("/api/criteria", headers={"X-HL-Token": rt.token})
    assert r.status_code == 200
    d = r.json()
    assert set(d) >= {"invariants", "domain_criteria", "layer3"}
    assert isinstance(d["invariants"], list) and isinstance(d["layer3"], dict)


def test_edit_all_three_layers(tmp_home, no_enforce):
    client, rt = _client(tmp_home)
    h = {"X-HL-Token": rt.token}
    with client:
        r = client.post("/api/criteria/layer1", headers=h, json={"invariants": ["A 금지", "B 금지"]})
        assert r.status_code == 200 and r.json()["invariants"] == ["A 금지", "B 금지"]

        r = client.post("/api/criteria/layer2", headers=h,
                        json={"domain_criteria": [{"description": "위험 명령 확인", "weight": 2.0}]})
        assert r.status_code == 200
        dc = r.json()["domain_criteria"]
        assert dc[0]["id"].startswith("DC-") and dc[0]["weight"] == 2.0

        r = client.post("/api/criteria/layer3", headers=h, json={"layer3": {"retry_threshold": 5}})
        assert r.status_code == 200 and r.json()["layer3"]["retry_threshold"] == 5


def test_edit_hot_reloads_runtime(tmp_home, no_enforce):
    """The in-memory criteria the next hook is judged against must reflect the edit at once."""
    client, rt = _client(tmp_home)
    with client:
        client.post("/api/criteria/layer1", headers={"X-HL-Token": rt.token}, json={"invariants": ["Z 금지"]})
    assert list(rt.criteria.invariants) == ["Z 금지"]


def test_edit_preserves_scopes(tmp_home, no_enforce):
    """Editing a base layer must not drop a project scope a user configured."""
    client, rt = _client(tmp_home)
    h = {"X-HL-Token": rt.token}
    with client:
        client.post("/api/scopes", headers=h, json={"scopes": [
            {"name": "proj", "match": {"cwd_prefix": "/tmp/p"}, "mode": "observe", "layer3": {}, "add_invariants": []}
        ]})
        client.post("/api/criteria/layer1", headers=h, json={"invariants": ["X 금지"]})
        r = client.get("/api/scopes", headers=h)
    assert any(s["name"] == "proj" for s in r.json()["scopes"])


def test_effective_criteria_and_flow_payload_are_project_scoped(tmp_home):
    client, rt = _client(tmp_home)
    h = {"X-HL-Token": rt.token}
    rt.save_scopes([
        {
            "name": "secure",
            "match": {"cwd_prefix": "/repo/secure"},
            "mode": "enforce",
            "layer3": {"retry_threshold": 1},
            "add_invariants": ["scope invariant"],
            "add_domain_criteria": [{"description": "scope criterion", "weight": 2.0}],
        }
    ])
    rt.ledger.upsert_flow(Flow(
        flow_id="F", source="codex", status="running", mode="observe", cwd="/repo/secure/app",
    ))

    with client:
        r = client.get("/api/criteria/effective?flow_id=F", headers=h)
        flows = client.get("/api/flows", headers=h).json()

    assert r.status_code == 200
    effective = r.json()
    assert effective["scope_name"] == "secure"
    assert effective["mode"] == "enforce"
    assert effective["layer3"]["retry_threshold"] == 1
    assert "scope invariant" in effective["invariants"]
    assert any(dc["description"] == "scope criterion" for dc in effective["domain_criteria"])

    flow = next(f for f in flows if f["flow_id"] == "F")
    assert flow["harness"]["scope_name"] == "secure"
    assert flow["harness"]["mode"] == "enforce"
    assert flow["harness"]["added_domain_criteria_count"] == 1


def test_edit_rejects_out_of_range_layer3(tmp_home, no_enforce):
    client, rt = _client(tmp_home)
    with client:
        r = client.post("/api/criteria/layer3", headers={"X-HL-Token": rt.token},
                        json={"layer3": {"retry_threshold": 0}})
    assert r.status_code == 400


def test_edit_unknown_layer_is_400(tmp_home):
    client, rt = _client(tmp_home)
    with client:
        r = client.post("/api/criteria/layerX", headers={"X-HL-Token": rt.token}, json={})
    assert r.status_code == 400


def test_criteria_requires_token(tmp_home):
    client, _rt = _client(tmp_home)
    with client:
        assert client.get("/api/criteria").status_code == 403
        assert client.post("/api/criteria/layer1", json={"invariants": []}).status_code == 403


def test_ui_serves_harness_editor(tmp_home):
    client, rt = _client(tmp_home)
    with client:
        r = client.get("/ui", headers={"host": "127.0.0.1"})
    assert r.status_code == 200
    # The redesigned page carries the harness editor + request-centric surface.
    for needle in (
        "renderHarness", "api/criteria", "프로젝트 하네스", "openHarness", "cleanTitle",
        "harnessName", "scopeDcRow",
    ):
        assert needle in r.text
