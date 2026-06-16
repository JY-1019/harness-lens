"""Team governance Phase 1 — a repo-committed .harness-lens/policy.yaml applies to sessions in it."""

from __future__ import annotations

from pathlib import Path

from harness_lens.criteria import find_repo_policy, load_repo_policy, repo_root_of
from harness_lens.daemon.events import HarnessEvent
from harness_lens.daemon.runtime import DaemonRuntime


def _write_policy(repo: Path, body: str) -> None:
    (repo / ".harness-lens").mkdir(parents=True, exist_ok=True)
    (repo / ".harness-lens" / "policy.yaml").write_text(body, encoding="utf-8")


def test_find_walks_up_to_repo_root(tmp_path):
    repo = tmp_path / "repo"
    _write_policy(repo, "invariants:\n  - no prod delete\n")
    deep = repo / "services" / "payments"
    deep.mkdir(parents=True)
    found = find_repo_policy(str(deep))
    assert found is not None
    assert Path(repo_root_of(found)).resolve() == repo.resolve()
    # Nothing up the tree → None.
    assert find_repo_policy(str(tmp_path / "elsewhere")) is None


def test_load_parses_flat_file_into_scope(tmp_path):
    repo = tmp_path / "repo"
    _write_policy(repo,
        "name: payments-governance\n"
        "invariants:\n  - 프로덕션 DB에 직접 DELETE 금지\n"
        "domain_criteria:\n"
        "  - id: BIZ-1\n    description: 배포 전 회귀 테스트를 동반한다\n    weight: 1.5\n"
        "layer3:\n  failure_count_trigger: 2\n"
        "mode: enforce\n")
    scope = load_repo_policy(find_repo_policy(str(repo)))
    assert scope is not None
    assert scope.name == "payments-governance"
    assert scope.mode == "enforce"
    assert "프로덕션 DB에 직접 DELETE 금지" in scope.add_invariants
    assert any(dc.id == "BIZ-1" for dc in scope.add_domain_criteria)
    assert scope.layer3.get("failure_count_trigger") == 2
    assert Path(scope.cwd_prefix).resolve() == repo.resolve()  # matches the whole repo


def test_load_is_lenient_on_bad_file(tmp_path):
    repo = tmp_path / "repo"
    _write_policy(repo, ":\n  - not: valid: yaml: [")
    assert load_repo_policy(repo / ".harness-lens" / "policy.yaml") is None


def test_repo_policy_composes_into_effective_criteria(tmp_home, tmp_path):
    repo = tmp_path / "repo"
    _write_policy(repo,
        "invariants:\n  - 거버넌스 전용 규칙\n"
        "domain_criteria:\n  - id: GOV-1\n    description: 결제 변경 시 회귀 테스트 동반\n    weight: 1\n"
        "mode: enforce\n")
    rt = DaemonRuntime(root=tmp_home)  # a clean personal home with NO scope for this repo
    eff = rt.effective_criteria_payload(cwd=str(repo / "app"))
    assert "거버넌스 전용 규칙" in eff["invariants"]                 # repo invariant present
    assert any(d["description"] == "결제 변경 시 회귀 테스트 동반" for d in eff["domain_criteria"])
    assert eff["repo_policy"] is not None                          # surfaced for the GUI
    assert eff["mode"] == "enforce"                                # repo pins the mode
    # A session OUTSIDE the repo is unaffected.
    assert "거버넌스 전용 규칙" not in rt.effective_criteria_payload(cwd=str(tmp_path))["invariants"]


def test_repo_policy_gates_enforcement(tmp_home, tmp_path):
    repo = tmp_path / "repo"
    _write_policy(repo,
        "domain_criteria:\n  - id: GOV-1\n    description: 배포 전 회귀 테스트를 동반한다\n    weight: 1\n"
        "mode: enforce\n")
    rt = DaemonRuntime(root=tmp_home)
    ev = HarnessEvent(source="codex", kind="pre_tool_use", session_id="s",
                      tool_name="Bash", tool_input={"command": "git commit -m ship"}, cwd=str(repo))
    engine, mode = rt._policy_for(ev)
    assert mode == "enforce"                                       # from the repo policy, not home
    decision = engine.evaluate_pre_tool(ev, mode)                  # no prior test run in context
    assert decision.action == "escalate" and decision.layer == 2
    assert decision.criterion_id == "GOV-1"                        # the repo-provided criterion fired
