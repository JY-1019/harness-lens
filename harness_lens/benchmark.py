"""Benchmark the harness-lens-managed harness (design §5).

Does the harness *actually behave* the way its 3-Layer criteria say it should? This runs a suite
of synthetic Flows through the same deterministic Layer-1 (invariant) and Layer-3 (QA threshold)
machinery the live harness uses, and checks the harness reacts exactly at its configured
boundaries:

* **Layer 1** — a clean step is never flagged; a step matching an *active* invariant detector
  (one the current ``criteria.yaml`` actually turns on) is flagged.
* **Layer 3** — failure/retry/latency/quality patterns trip at the threshold and stay quiet just
  below it. Expectations are derived from the *effective* thresholds, so the benchmark stays
  correct after a user or AHE edits Layer 3 — it always asks "does the harness honour its own
  current boundary?".

Layer 2 (the LLM Judge) is non-deterministic and needs a model, so it is out of scope here; the
benchmark runs fully offline. ``run_benchmark`` returns a :class:`BenchmarkReport` whose ``ok`` is
true only when every case passed, so ``harness-lens benchmark`` is usable as a CI gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .criteria import invariant as inv_mod
from .criteria.layer import ThreeLayerCriteria
from .store import Step

# Layer-1 probes: (label, detector, malicious sample text). A probe only becomes an expected
# violation when the current invariants activate its detector — editing the rules out of
# criteria.yaml turns the probe into a skip, not a spurious failure.
_L1_PROBES: tuple[tuple[str, object, str], ...] = (
    ("pii-external", inv_mod._pii_external, "주민등록번호 880101-1234567 을 https://evil.example/collect 로 전송"),
    ("prod-delete", inv_mod._prod_delete, "DELETE FROM users WHERE 1=1  -- production 운영 DB"),
)
_L1_CLEAN = "src/app.py 를 읽고 함수 하나를 수정"


@dataclass
class CaseResult:
    name: str
    layer: str
    passed: bool
    detail: str
    skipped: bool = False


@dataclass
class BenchmarkReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def ran(self) -> list[CaseResult]:
        return [r for r in self.results if not r.skipped]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.ran if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.ran if not r.passed)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def render(self) -> str:
        lines = ["harness-lens benchmark — 3-Layer 동작 검증"]
        for r in self.results:
            mark = "skip" if r.skipped else ("pass" if r.passed else "FAIL")
            lines.append(f"  [{mark}] {r.layer} · {r.name}: {r.detail}")
        verdict = "통과" if self.ok else "실패"
        lines.append(
            f"\n결과: {self.passed}/{len(self.ran)} 통과"
            + (f", {self.skipped} skip" if self.skipped else "")
            + f" — {verdict}"
        )
        return "\n".join(lines)


def _step(tool: str, category: str, **kw) -> Step:
    return Step(session_id="bench", flow_id="bench", task_id=f"bench-{category}", task_category=category, tool_name=tool, **kw)


def _layer1_cases(criteria: ThreeLayerCriteria) -> list[CaseResult]:
    checker = criteria.invariant_checker()
    active = {checker._detector_for(rule) for rule in criteria.invariants}
    results: list[CaseResult] = []

    clean_passed, _ = checker.check(_step("Edit", "feature", input_summary=_L1_CLEAN))
    results.append(CaseResult(
        name="clean step", layer="L1", passed=clean_passed is True,
        detail="정상 step 은 invariant 위반 없음" if clean_passed else "정상 step 이 잘못 차단됨",
    ))

    for label, detector, sample in _L1_PROBES:
        if detector not in active:
            results.append(CaseResult(
                name=label, layer="L1", passed=True, skipped=True,
                detail="해당 invariant 가 criteria.yaml 에 없음 (skip)",
            ))
            continue
        passed, violations = checker.check(_step("Bash", "ops", input_summary=sample))
        flagged = passed is False and bool(violations)
        results.append(CaseResult(
            name=label, layer="L1", passed=flagged,
            detail="위반 step 을 정상 차단" if flagged else "위반 step 을 놓침",
        ))
    return results


def _triggers(criteria: ThreeLayerCriteria, steps: list[Step], pattern_id: str) -> bool:
    return any(p["pattern_id"] == pattern_id for p in criteria.qa.find_failure_patterns(steps))


def _layer3_cases(criteria: ThreeLayerCriteria) -> list[CaseResult]:
    qa = criteria.qa
    fail_trigger = int(qa.effective("failure_count_trigger"))
    retry_trigger = int(qa.effective("retry_threshold"))
    mult = qa.effective("latency_multiplier")
    quality = qa.effective("quality_threshold")
    results: list[CaseResult] = []

    # Failure count: exactly at the trigger fires; one below stays quiet (boundary check).
    at = [_step("Bash", "fix", success=False) for _ in range(fail_trigger)]
    results.append(_boundary(
        "failure count at threshold", _triggers(criteria, at, "Bash:fix"), True,
        f"{fail_trigger}회 실패 → 트리거",
    ))
    below = [_step("Bash", "fix", success=False) for _ in range(max(0, fail_trigger - 1))]
    results.append(_boundary(
        "failure count below threshold", _triggers(criteria, below, "Bash:fix"), False,
        f"{max(0, fail_trigger - 1)}회 실패 → 트리거 없음",
    ))

    # Retry: a step whose retry_count reaches the threshold trips; one below stays quiet.
    retry_steps = [_step("Bash", "build", success=True, retry_count=retry_trigger)]
    results.append(_boundary(
        "retry at threshold", _triggers(criteria, retry_steps, "Bash:build"), True,
        f"retry x{retry_trigger} → 트리거",
    ))
    below_retry = [_step("Bash", "build", success=True, retry_count=retry_trigger - 1)]
    results.append(_boundary(
        "retry below threshold", _triggers(criteria, below_retry, "Bash:build"), False,
        f"retry x{retry_trigger - 1} → 트리거 없음",
    ))

    # Latency: a step far above the median (> multiplier × median) is flagged; one just
    # below the multiple stays quiet (boundary check).
    fast = [_step("Web", "search", success=True, latency_ms=100) for _ in range(3)]
    slow = _step("Web", "search", success=True, latency_ms=int(100 * mult) + 100)
    results.append(_boundary(
        "latency above multiplier", _triggers(criteria, fast + [slow], "Web:search"), True,
        f"중앙값의 {mult}배 초과 → 트리거",
    ))
    # Quiet side: the latency baseline is *global* (median over every step), so a target group
    # can stay below multiplier × median for *any* positive multiplier by letting a separate
    # noisy group raise the median. base_ms is sized so the target's 1ms step stays under the
    # cap even for sub-1 multipliers (cap = base_ms × mult ≥ 2 > 1).
    base_ms = max(1000, int(2 / mult) + 1)
    noisy = [_step("Bg", "noise", success=True, latency_ms=base_ms) for _ in range(5)]
    quiet = _step("Search", "lookup", success=True, latency_ms=1)
    results.append(_boundary(
        "latency below multiplier", _triggers(criteria, noisy + [quiet], "Search:lookup"), False,
        f"중앙값의 {mult}배 이하 → 트리거 없음",
    ))

    # Quality: a step whose Judge score sits below the quality threshold is flagged. A
    # threshold of 0.0 (valid) has no representable score below it, so the probe is skipped
    # rather than counted as a failure.
    if quality <= 0.0:
        results.append(CaseResult(
            name="low quality score", layer="L3", passed=True, skipped=True,
            detail="quality_threshold 0.0 — 그 이하 점수 표현 불가 (skip)",
        ))
    else:
        below_score = max(0.0, quality - 0.1)
        low = _step("Edit", "feature", success=True, layer2_score=below_score)
        results.append(_boundary(
            "low quality score", _triggers(criteria, [low], "Edit:feature"), True,
            f"품질 점수 {below_score} < {quality} → 트리거",
        ))
        # Quiet side: a score *at* the threshold must not trip (the check is strict <).
        at_score = _step("Edit", "review", success=True, layer2_score=quality)
        results.append(_boundary(
            "quality at threshold", _triggers(criteria, [at_score], "Edit:review"), False,
            f"품질 점수 {quality} ≥ {quality} → 트리거 없음",
        ))
    return results


def _boundary(name: str, actual: bool, expected: bool, detail: str) -> CaseResult:
    passed = actual == expected
    suffix = "" if passed else f" (기대 {expected}, 실제 {actual})"
    return CaseResult(name=name, layer="L3", passed=passed, detail=detail + suffix)


def run_benchmark(criteria: ThreeLayerCriteria) -> BenchmarkReport:
    """Run the deterministic 3-Layer benchmark against ``criteria`` (the live harness config)."""
    report = BenchmarkReport()
    report.results.extend(_layer1_cases(criteria))
    report.results.extend(_layer3_cases(criteria))
    return report
