"""Unit tests for the rule compiler — built-in detector detection, LLM classification, and the
regex self-verification that keeps an unverified (hallucinated) detector from being trusted to gate.
"""

from __future__ import annotations

from harness_lens.compiler import (
    builtin_invariant_detector,
    builtin_l2_detector,
    classify_rules,
    verify_regex,
)


class _StubLLM:
    """A canned LLM whose reply is a fixed JSON array, so classify_rules runs without a real call."""

    def __init__(self, reply: str):
        self._reply = reply

    def ensure_ready(self) -> None:  # mirrors the LLMClient surface compiler probes
        pass

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        return self._reply


def test_builtin_detection_maps_known_rules():
    assert builtin_invariant_detector("비밀키를 코드에 하드코딩하지 않는다") == "_secret_hardcode"
    assert builtin_invariant_detector("force-push 로 보호 브랜치를 덮어쓰지 않는다") == "_force_push"
    assert builtin_l2_detector("동작 변경은 회귀 테스트를 동반한다") == "_l2_test_before_change"
    # A purely semantic rule maps to no built-in detector.
    assert builtin_invariant_detector("응답은 항상 공손해야 한다") is None
    assert builtin_l2_detector("응답은 항상 공손해야 한다") is None


def test_verify_regex_gates_only_on_passing_examples():
    assert verify_regex(r"foo\s+bar", {"positive": ["foo bar"], "negative": ["baz"]})["verified"] is True
    # A negative example that matches must fail verification.
    assert verify_regex(r"foo", {"positive": ["foo"], "negative": ["food"]})["verified"] is False
    # No positive example → not verified (cannot trust a regex that never demonstrably matches).
    assert verify_regex(r"foo", {"positive": [], "negative": []})["verified"] is False
    # An invalid regex never verifies (and never raises).
    assert verify_regex("(", {"positive": ["x"]})["verified"] is False


def test_builtin_rules_need_no_llm():
    report = classify_rules(["비밀키를 코드에 하드코딩하지 않는다"], [], llm=None)
    assert report["llm_used"] is False
    assert report["rules"][0]["enforcement"] == "detector"
    assert report["rules"][0]["builtin_detector"] == "_secret_hardcode"


def test_classify_splits_detector_and_semantic():
    reply = (
        '[{"index":0,"classification":"semantic","reason":"판단 필요","regex":null,"examples":{}},'
        '{"index":1,"classification":"deterministic","reason":"패턴","regex":"foo\\\\s+bar",'
        '"examples":{"positive":["foo bar"],"negative":["baz"]}}]'
    )
    invariants = ["비밀키를 코드에 하드코딩하지 않는다", "응답은 항상 공손해야 한다"]
    domain = [
        {"id": "D1", "description": "동작 변경은 회귀 테스트를 동반한다"},
        {"id": "D2", "description": "임시 파일은 작업 후 정리한다"},
    ]
    report = classify_rules(invariants, domain, llm=_StubLLM(reply))
    by = {r["text"]: r for r in report["rules"]}

    # Built-in matches gate without the LLM.
    assert by["비밀키를 코드에 하드코딩하지 않는다"]["enforcement"] == "detector"
    assert by["동작 변경은 회귀 테스트를 동반한다"]["builtin_detector"] == "_l2_test_before_change"
    # Semantic → advisory; deterministic+verified regex → detector.
    assert by["응답은 항상 공손해야 한다"]["enforcement"] == "advisory"
    assert by["응답은 항상 공손해야 한다"]["classification"] == "semantic"
    d2 = by["임시 파일은 작업 후 정리한다"]
    assert d2["classification"] == "deterministic"
    assert d2["proposed"]["verified"] is True and d2["enforcement"] == "detector"
    assert report["llm_used"] is True
    assert report["counts"]["detector"] == 3 and report["counts"]["advisory"] == 1


def test_unverified_regex_stays_advisory():
    """A deterministic verdict whose regex fails its own examples must NOT be promoted to a gate."""
    reply = (
        '[{"index":0,"classification":"deterministic","reason":"x","regex":"zzz",'
        '"examples":{"positive":["abc"],"negative":[]}}]'
    )
    report = classify_rules(["응답은 항상 공손해야 한다"], [], llm=_StubLLM(reply))
    rule = report["rules"][0]
    assert rule["proposed"]["verified"] is False
    assert rule["enforcement"] == "advisory"


def test_malformed_llm_reply_falls_back_to_advisory():
    report = classify_rules(["응답은 항상 공손해야 한다"], [], llm=_StubLLM("not json at all"))
    assert report["rules"][0]["enforcement"] == "advisory"
    assert report["rules"][0]["classification"] == "unknown"


def test_compiled_policy_is_exportable_and_annotated():
    from harness_lens.compiler import compiled_policy

    rules = [
        {"layer": 1, "id": None, "text": "비밀키를 코드에 하드코딩하지 않는다",
         "builtin_detector": "_secret_hardcode", "classification": "deterministic",
         "enforcement": "detector", "proposed": None},
        {"layer": 1, "id": None, "text": "응답은 공손해야 한다", "builtin_detector": None,
         "classification": "semantic", "enforcement": "advisory", "proposed": None},
        {"layer": 2, "id": "D1", "text": "임시 파일은 정리한다", "builtin_detector": None,
         "classification": "deterministic", "enforcement": "detector",
         "proposed": {"regex": "tmp", "verified": True}},
    ]
    pol = compiled_policy(rules, name="demo")
    # Standard repo-policy fields so the daemon/CI already understand it.
    assert pol["name"] == "demo"
    assert pol["invariants"] == ["비밀키를 코드에 하드코딩하지 않는다", "응답은 공손해야 한다"]
    assert pol["domain_criteria"][0]["id"] == "D1"
    # The `compiled` annotation records how each rule is enforced.
    comp = {c["text"]: c for c in pol["compiled"]}
    assert comp["비밀키를 코드에 하드코딩하지 않는다"]["detector"] == "builtin:_secret_hardcode"
    assert comp["응답은 공손해야 한다"]["enforcement"] == "advisory"
    g = comp["임시 파일은 정리한다"]
    assert g["detector"] == "generated" and g["regex"] == "tmp" and g["verified"] is True


def test_compiled_policy_autogenerates_l2_ids():
    from harness_lens.compiler import compiled_policy

    rules = [{"layer": 2, "id": None, "text": "어떤 기준", "builtin_detector": None,
              "classification": "semantic", "enforcement": "advisory", "proposed": None}]
    pol = compiled_policy(rules)
    assert pol["domain_criteria"][0]["id"] == "DC-001"


def test_compiled_policy_emits_only_verified_detectors():
    """A `detectors:` section (what the engine loads to gate) carries only verified-generated regexes."""
    from harness_lens.compiler import compiled_policy

    rules = [
        {"layer": 1, "text": "A", "builtin_detector": None, "classification": "deterministic",
         "enforcement": "detector", "proposed": {"regex": "aaa", "verified": True}},
        {"layer": 2, "id": "X", "text": "B", "builtin_detector": None, "classification": "deterministic",
         "enforcement": "advisory", "proposed": {"regex": "bbb", "verified": False}},  # excluded
    ]
    pol = compiled_policy(rules)
    assert [d["regex"] for d in pol["detectors"]] == ["aaa"]
    assert pol["detectors"][0]["verified"] is True


def test_generated_detector_escalates_in_enforce_only():
    """A verified generated detector gates (escalate) in enforce, but never in observe / on no-match."""
    from harness_lens.criteria.layer import ThreeLayerCriteria, parse_detectors
    from harness_lens.criteria.qa import QACriteria
    from harness_lens.daemon.config import MODE_ENFORCE, MODE_OBSERVE
    from harness_lens.daemon.events import HarnessEvent
    from harness_lens.daemon.policy import PolicyContext, PolicyEngine

    dets = parse_detectors([
        {"layer": 1, "rule": "임시 자격증명 금지", "regex": "temp_pass", "verified": True},
        {"layer": 2, "rule": "unverified", "regex": "zzz", "verified": False},  # dropped
        {"layer": 2, "rule": "broken", "regex": "(", "verified": True},          # dropped (bad regex)
    ])
    assert len(dets) == 1

    engine = PolicyEngine(ThreeLayerCriteria(invariants=[], domain_criteria=[], qa=QACriteria(),
                                             generated_detectors=dets))
    hit = HarnessEvent(source="claude_code", kind="pre_tool_use", session_id="s",
                       tool_name="Bash", tool_input={"command": "echo temp_pass1"})
    miss = HarnessEvent(source="claude_code", kind="pre_tool_use", session_id="s",
                        tool_name="Bash", tool_input={"command": "echo ok"})

    d = engine.evaluate_pre_tool(hit, MODE_ENFORCE, PolicyContext())
    assert d.action == "escalate" and d.layer == 1
    assert engine.evaluate_pre_tool(miss, MODE_ENFORCE, PolicyContext()).action == "allow"
    assert engine.evaluate_pre_tool(hit, MODE_OBSERVE, PolicyContext()).action == "allow"


def test_generated_detectors_compose_through_scope():
    """A repo policy's `detectors:` compose additively onto the base via apply_scope."""
    from harness_lens.criteria.layer import ThreeLayerCriteria
    from harness_lens.criteria.qa import QACriteria
    from harness_lens.criteria.scope import apply_scope, parse_scopes

    base = ThreeLayerCriteria(invariants=[], domain_criteria=[], qa=QACriteria())
    scope = parse_scopes([{
        "name": "p", "match": {"cwd_prefix": "/x"},
        "detectors": [{"layer": 1, "rule": "r", "regex": "abc", "verified": True}],
    }])[0]
    eff = apply_scope(base, scope)
    assert len(eff.generated_detectors) == 1 and eff.generated_detectors[0].regex == "abc"
