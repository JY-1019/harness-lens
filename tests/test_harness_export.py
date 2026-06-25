"""Export a project's harness to a portable file and swap it into another project."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from harness_lens.daemon.app import create_app
from harness_lens.daemon.runtime import DaemonRuntime


def _client(tmp_home):
    rt = DaemonRuntime(root=tmp_home)
    return TestClient(create_app(runtime=rt)), rt


def test_export_effective_is_a_flat_portable_policy(tmp_home):
    client, rt = _client(tmp_home)
    rt.save_scopes([{
        "name": "payments", "match": {"cwd": "/repo/pay"}, "mode": "enforce",
        "add_invariants": ["프로덕션 DB DELETE 금지"],
        "add_domain_criteria": [{"id": "BIZ-1", "description": "회귀 테스트 동반", "weight": 1.5}],
        "layer3": {"failure_count_trigger": 2},
    }])
    h = {"X-HL-Token": rt.token}
    with client:
        r = client.get("/api/harness/export?cwd=/repo/pay&format=yaml", headers=h)
    assert r.status_code == 200
    out = r.json()
    assert out["filename"] == "pay.harness.yaml"
    policy = yaml.safe_load(out["content"])
    assert "프로덕션 DB DELETE 금지" in policy["invariants"]            # the scope rule is baked in
    assert any(d["id"] == "BIZ-1" for d in policy["domain_criteria"])
    assert policy["mode"] == "enforce"
    assert policy["layer3"]["failure_count_trigger"] == 2


def test_apply_repo_writes_committable_policy_file(tmp_home, tmp_path):
    client, rt = _client(tmp_home)
    repo = tmp_path / "proj"
    repo.mkdir()
    policy = {"name": "p", "invariants": ["거버넌스 규칙"],
              "domain_criteria": [{"id": "GOV-1", "description": "배포 전 회귀 테스트 동반", "weight": 1}],
              "layer3": {"failure_count_trigger": 2}, "mode": "enforce"}
    h = {"X-HL-Token": rt.token}
    with client:
        r = client.post("/api/harness/apply", headers=h,
                        json={"cwd": str(repo), "policy": policy, "target": "repo"})
        assert r.status_code == 200 and r.json()["target"] == "repo"
        # the file is written where the daemon auto-discovers it …
        written = repo / ".harness-lens" / "policy.yaml"
        assert written.is_file()
        # … and the project's effective harness now carries it (hot-reload, no restart)
        eff = client.get(f"/api/criteria/effective?cwd={repo}", headers=h).json()
    assert "거버넌스 규칙" in eff["invariants"]
    assert eff["mode"] == "enforce"
    assert eff["repo_policy"] is not None


def test_apply_scope_installs_personal_overlay(tmp_home):
    client, rt = _client(tmp_home)
    h = {"X-HL-Token": rt.token}
    policy = {"name": "local", "invariants": ["로컬 규칙"], "domain_criteria": [], "layer3": {}}
    with client:
        r = client.post("/api/harness/apply", headers=h,
                        json={"cwd": "/repo/x", "policy": policy, "target": "scope"})
        assert r.status_code == 200 and r.json()["target"] == "scope"
        scopes = client.get("/api/scopes", headers=h).json()["scopes"]
        eff = client.get("/api/criteria/effective?cwd=/repo/x", headers=h).json()
    assert any((s.get("match") or {}).get("cwd") == "/repo/x" for s in scopes)
    assert "로컬 규칙" in eff["invariants"]


def test_apply_accepts_raw_yaml_content(tmp_home, tmp_path):
    client, rt = _client(tmp_home)
    repo = tmp_path / "y"
    repo.mkdir()
    content = "name: t\ninvariants:\n  - raw 규칙\ndomain_criteria: []\nlayer3: {}\n"
    h = {"X-HL-Token": rt.token}
    with client:
        r = client.post("/api/harness/apply", headers=h,
                        json={"cwd": str(repo), "content": content, "target": "repo"})
        assert r.status_code == 200
        eff = client.get(f"/api/criteria/effective?cwd={repo}", headers=h).json()
    assert "raw 규칙" in eff["invariants"]


def test_export_then_apply_roundtrip_reproduces_harness(tmp_home, tmp_path):
    """Pull project A's harness out and swap it into project B — B behaves like A."""
    client, rt = _client(tmp_home)
    rt.save_scopes([{
        "name": "A", "match": {"cwd": "/repo/A"}, "mode": "enforce",
        "add_invariants": ["A 전용 규칙"],
        "add_domain_criteria": [{"id": "A-1", "description": "배포 전 회귀 테스트 동반", "weight": 1}],
        "layer3": {"failure_count_trigger": 2},
    }])
    repo_b = tmp_path / "B"
    repo_b.mkdir()
    h = {"X-HL-Token": rt.token}
    with client:
        exported = client.get("/api/harness/export?cwd=/repo/A&format=yaml", headers=h).json()
        client.post("/api/harness/apply", headers=h,
                    json={"cwd": str(repo_b), "content": exported["content"], "target": "repo"})
        eff_b = client.get(f"/api/criteria/effective?cwd={repo_b}", headers=h).json()
    assert "A 전용 규칙" in eff_b["invariants"]
    assert any(d["id"] == "A-1" for d in eff_b["domain_criteria"])
    assert eff_b["mode"] == "enforce"
