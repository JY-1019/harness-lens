"""Flow-level user requests are captured and surfaced, and the human owner can edit every layer.

Two improvements are covered:

* The reconstructor now persists each ``UserPromptSubmit`` durably (the cursor only held the
  latest, transiently) and links it to the Task its first tool call opens, so the GUI's flow tree
  can show *what was asked* per Flow and per Task.
* ``LensService`` exposes explicit human-owner edits of Layer 1 (invariants) and Layer 2 (domain
  criteria) — distinct from AHE auto-evolution, which the CriteriaGuard still pins to Layer 3.
"""

from __future__ import annotations

import pytest

from harness_lens.reconstructor import CodexReconstructor, Reconstructor
from harness_lens.service import LensService


@pytest.fixture
def no_enforce(monkeypatch):
    """Keep criteria edits from touching the developer's real CLAUDE.md / AGENTS.md.

    ``_write_criteria`` re-enforces the managed instruction block on every *detected* platform;
    stubbing detection to none keeps the write confined to the isolated tmp_home criteria.yaml.
    """
    import harness_lens.detector as detector

    monkeypatch.setattr(detector, "detect_all", lambda: [])


# --------------------------------------------------------------------------- #
# Requests are captured and surfaced
# --------------------------------------------------------------------------- #
def test_prompt_persisted_and_linked_to_task(tmp_home):
    svc = LensService(root=tmp_home)
    recon = Reconstructor(svc.store)
    sid = "S-req"
    recon.on_session_start(sid, platform="claude-code")
    recon.on_user_prompt(sid, "테스트 실패를 고쳐줘")
    recon.on_pre_tool(sid, "Read", "pytest output")
    recon.on_post_tool(sid, "Read", "ok", success=True, latency_ms=5)

    prompts = svc.store.prompts_for_session(sid)
    assert len(prompts) == 1
    assert prompts[0].text == "테스트 실패를 고쳐줘"
    assert prompts[0].task_id is not None  # linked to the Task its first tool opened

    flow = svc.get_flow_summary(session_id=sid)[0]
    assert [r["text"] for r in flow["requests"]] == ["테스트 실패를 고쳐줘"]
    assert flow["tasks"][0]["request"] == "테스트 실패를 고쳐줘"
    svc.close()


def test_two_prompts_map_to_two_tasks(tmp_home):
    svc = LensService(root=tmp_home)
    recon = Reconstructor(svc.store)
    sid = "S-two"
    recon.on_session_start(sid)
    recon.on_user_prompt(sid, "첫 번째 요청")
    recon.on_pre_tool(sid, "Read", "a")
    recon.on_post_tool(sid, "Read", "ok", success=True)
    recon.on_user_prompt(sid, "두 번째 요청")
    recon.on_pre_tool(sid, "Bash", "ls")
    recon.on_post_tool(sid, "Bash", "ok", success=True)

    flow = svc.get_flow_summary(session_id=sid)[0]
    assert [r["text"] for r in flow["requests"]] == ["첫 번째 요청", "두 번째 요청"]
    # Each Task carries the request that opened it (order = first-step order).
    assert {t["request"] for t in flow["tasks"]} == {"첫 번째 요청", "두 번째 요청"}
    svc.close()


def test_long_prompt_is_bounded_but_kept(tmp_home):
    svc = LensService(root=tmp_home)
    recon = Reconstructor(svc.store)
    sid = "S-long"
    recon.on_session_start(sid)
    recon.on_user_prompt(sid, "x" * 9000)
    prompts = svc.store.prompts_for_session(sid)
    assert len(prompts) == 1
    # Stored far more than a 160-char task name, but still bounded.
    assert 160 < len(prompts[0].text) <= 4000
    svc.close()


def test_codex_prompt_is_recorded(tmp_home):
    svc = LensService(root=tmp_home)
    recon = CodexReconstructor(svc.store)
    sid = "S-codex"
    recon.on_session_start(sid, platform="codex")
    recon.on_user_prompt(sid, "코덱스 요청")
    recon.on_pre_tool(sid, "Bash", "echo hi")
    recon.on_post_tool(sid, "Bash", "hi", success=True)
    flow = svc.get_flow_summary(session_id=sid)[0]
    assert [r["text"] for r in flow["requests"]] == ["코덱스 요청"]
    svc.close()


# --------------------------------------------------------------------------- #
# Human owner edits of Layer 1 / Layer 2
# --------------------------------------------------------------------------- #
def test_update_invariants_persists_and_dedupes(tmp_home, no_enforce):
    svc = LensService(root=tmp_home)
    svc.update_invariants(["A를 하지 않는다", "   ", "B를 하지 않는다", "A를 하지 않는다"])
    assert svc.criteria.invariants == ["A를 하지 않는다", "B를 하지 않는다"]

    # Persisted to criteria.yaml and reloadable by a fresh service.
    reloaded = LensService(root=tmp_home)
    assert reloaded.criteria.invariants == ["A를 하지 않는다", "B를 하지 않는다"]
    svc.close()
    reloaded.close()


def test_update_invariants_allows_empty(tmp_home, no_enforce):
    svc = LensService(root=tmp_home)
    svc.update_invariants([])
    assert svc.criteria.invariants == []
    svc.close()


def test_update_domain_criteria_edit_add_preserve_prompt(tmp_home, no_enforce):
    svc = LensService(root=tmp_home)
    # DC-001 ships in the default criteria with its own judge_prompt; reweight it without
    # re-supplying the prompt, and add a brand-new criterion with no id.
    original_prompt = next(c.judge_prompt for c in svc.criteria.domain_criteria if c.id == "DC-001")
    view = svc.update_domain_criteria([
        {"id": "DC-001", "description": "파일 수정 전 현재 내용을 먼저 읽어야 한다", "weight": 2.0},
        {"description": "위험한 명령은 확인을 받는다", "weight": 1.0},
    ])
    ids = [d["id"] for d in view["domain_criteria"]]
    assert ids == ["DC-001", "DC-002"]  # missing id auto-assigned, no collision
    assert view["domain_criteria"][0]["weight"] == 2.0

    crit = {c.id: c for c in svc.criteria.domain_criteria}
    # Preserved on edit (normalised: surrounding whitespace trimmed).
    assert crit["DC-001"].judge_prompt == original_prompt.strip()
    assert crit["DC-002"].judge_prompt  # synthesised default for the new criterion
    svc.close()


def test_update_domain_criteria_drops_blank_description(tmp_home, no_enforce):
    svc = LensService(root=tmp_home)
    svc.update_domain_criteria([
        {"id": "DC-001", "description": "유효 기준", "weight": 1.0},
        {"id": "DC-009", "description": "   ", "weight": 1.0},
    ])
    assert [c.id for c in svc.criteria.domain_criteria] == ["DC-001"]
    svc.close()


def test_layer3_still_guarded_against_out_of_range(tmp_home, no_enforce):
    """AHE's evolvable layer keeps its range checks even on the human edit path."""
    from harness_lens.components import ComponentError

    svc = LensService(root=tmp_home)
    with pytest.raises(ComponentError):
        svc.update_layer3({"retry_threshold": 0})  # below the allowed floor
    svc.close()
