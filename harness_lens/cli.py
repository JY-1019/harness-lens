"""Command-line interface.

    harness-lens install        wire into Claude Code + init runtime
    harness-lens show [--fail]  recent Flows (with Layer 2)
    harness-lens diagnose       Pillar 2 — Debugger agent
    harness-lens evolve         Pillar 3 — proposals (+ --apply ID --yes)
    harness-lens verify         verify predictions → confirm / roll back
    harness-lens review         Judge labelling
    harness-lens rollback       revert last applied change
    harness-lens status         3-Layer + prediction hit-rate + Judge
    harness-lens serve          run the MCP server
    harness-lens hook <event>   internal: receive a harness hook event
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .llm import LLMUnavailable
from .service import LensService

_STATUS_MARK = {"completed": "✅", "failed": "⚠", "active": "…"}


def _service() -> LensService:
    return LensService()


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #
def _render_flow(flow: dict) -> str:
    mark = _STATUS_MARK.get(flow["status"], "?")
    l2 = f"{flow['layer2_avg']:.2f}" if flow["layer2_avg"] is not None else "n/a"
    lines = [
        f"Flow {flow['session_id'][:8]}  [{flow['platform']}]  "
        f"tokens {flow['total_tokens']:,}  {mark}",
        f"  Layer 2: {l2}",
    ]
    for i, task in enumerate(flow["tasks"], 1):
        steps = task["steps"]
        retries = sum(s["retry_count"] for s in steps)
        fails = sum(1 for s in steps if s["success"] is False)
        flag = "✅" if fails == 0 else "⚠"
        extra = f"  (retry {retries})" if retries else ""
        lines.append(f"  Task {i} [{task['category']}]  {flag}  {len(steps)} steps{extra}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_install(args) -> int:
    from .hooks.install import install

    report = install(platform_name=args.platform)
    print(report.render())
    return 0


def cmd_show(args) -> int:
    service = _service()
    flows = service.get_flow_summary(limit=args.limit, only_failed=args.fail)
    if not flows:
        print("기록된 Flow가 없습니다. Claude Code에서 작업하면 자동 추적됩니다.")
        return 0
    print("\n\n".join(_render_flow(f) for f in flows))
    return 0


def cmd_diagnose(args) -> int:
    service = _service()
    try:
        diagnoses = service.run_diagnosis()
    except LLMUnavailable as exc:
        print(f"진단에는 LLM이 필요합니다: {exc}", file=sys.stderr)
        return 1
    if not diagnoses:
        print("진단할 실패 패턴이 없습니다.")
        return 0
    for d in diagnoses:
        target = d["affected_component"] or "(사람 검토 필요 — 블랙박스 내부)"
        print(f"• {d['failure_pattern']}: {d['diagnosis']}\n    대상: {target}")
    return 0


def cmd_evolve(args) -> int:
    service = _service()
    if args.apply:
        from .components import ComponentError
        from .criteria.layer import CriteriaViolation

        try:
            candidate = service.apply_evolution(args.apply, confirmed=args.yes)
        except (CriteriaViolation, ComponentError) as exc:
            # ComponentError covers payloads that pass the guard but cannot be applied
            # (non-JSON hooks content, no detected live target); refuse cleanly, no traceback.
            print(f"적용 거부: {exc}", file=sys.stderr)
            return 1
        print(f"적용됨: {candidate['candidate_id']} (status={candidate['status']})")
        return 0

    try:
        proposals = service.propose_evolution()
    except LLMUnavailable as exc:
        print(f"진화 제안에는 LLM이 필요합니다: {exc}", file=sys.stderr)
        return 1
    if not proposals:
        print("제안할 수정안이 없습니다.")
        return 0
    for p in proposals:
        if "held" in p:
            print(f"• {p['failure_pattern']}: 보류 — {p['held']}")
            continue
        print(
            f"• {p['candidate_id']}  [{p['target_component']} / L{p['target_layer']}]\n"
            f"    진단: {p['diagnosis']}\n"
            f"    예측: {p['prediction']} ({p['predicted_metric']} → {p['predicted_value']})\n"
            f"    적용: harness-lens evolve --apply {p['candidate_id']} --yes"
        )
    return 0


def cmd_verify(args) -> int:
    service = _service()
    results = service.verify_predictions()
    if not results:
        print("검증할 적용된 수정안이 없습니다.")
        return 0
    for r in results:
        if r.was_correct is None:
            print(f"• {r.candidate_id}: 판정 보류 ({r.note})")
        else:
            verdict = "적중 → 확정" if r.was_correct else "빗나감 → 롤백"
            print(f"• {r.candidate_id}: {r.predicted_metric} 예측 {r.predicted_value} / 실제 {r.actual_value} — {verdict}")
    return 0


def cmd_review(args) -> int:
    service = _service()
    if args.sample is not None:
        # Require an explicit label: argparse used to default this to 1.0, so a user who
        # only meant to inspect a sample (or mistyped) would silently record it as passing,
        # removing it from the pending queue and biasing Judge agreement.
        if args.label is None:
            print("--sample 에는 --label <0..1> 이 필요합니다.", file=sys.stderr)
            return 1
        if not 0.0 <= args.label <= 1.0:
            print(f"--label 은 0..1 범위여야 합니다 (받은 값: {args.label})", file=sys.stderr)
            return 1
        samples = {s.sample_id: s for s in service.store.judge_samples()}
        sample = samples.get(args.sample)
        if sample is None:
            print(f"샘플을 찾을 수 없습니다: {args.sample}", file=sys.stderr)
            return 1
        service.label_sample(sample, args.label)
        status = service.get_judge_status()
        print(f"라벨 기록됨. {status.recommendation}")
        return 0

    pending = service.pending_reviews()
    if not pending:
        status = service.get_judge_status()
        print(f"라벨링 대기 중인 샘플이 없습니다. {status.recommendation}")
        return 0
    print("라벨링 대기 샘플 (--sample <id> --label <0..1>):")
    for s in pending:
        print(f"  {s.sample_id}  step={s.step_id[:8]}  judge={s.judge_score:.2f}")
    return 0


def cmd_rollback(args) -> int:
    service = _service()
    candidate = service.rollback_last()
    if candidate is None:
        print("롤백할 적용 내역이 없습니다.")
        return 0
    print(f"롤백됨: {candidate['candidate_id']} → {candidate['target_component']}")
    return 0


def cmd_status(args) -> int:
    service = _service()
    s = service.status()
    judge = s["judge"]
    hit = s["prediction_hit_rate"]
    print("harness-lens status")
    print(f"  Judge      : {judge.recommendation}")
    print(f"  예측 적중률 : {f'{hit:.0%}' if hit is not None else 'n/a'}")
    print(f"  Layer 3    : {s['layer3']}")
    print(f"  수정안      : {s['candidates']}")
    if not service.llm_available():
        print("  (참고) ANTHROPIC_API_KEY 미설정 — diagnose/evolve 비활성")
    return 0


def cmd_serve(args) -> int:
    from .server import main as serve_main

    return serve_main()


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-lens", description="Observe and evolve agentic harnesses.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="wire into Claude Code + init runtime")
    p_install.add_argument("--platform", default=None, help="force a platform id (default: auto-detect)")
    p_install.set_defaults(func=cmd_install)

    p_show = sub.add_parser("show", help="recent Flows")
    p_show.add_argument("--fail", action="store_true", help="only failed Flows")
    p_show.add_argument("--limit", type=int, default=20)
    p_show.set_defaults(func=cmd_show)

    sub.add_parser("diagnose", help="Pillar 2 diagnosis").set_defaults(func=cmd_diagnose)

    p_evolve = sub.add_parser("evolve", help="Pillar 3 proposals")
    p_evolve.add_argument("--apply", default=None, metavar="CAND_ID", help="apply a candidate")
    p_evolve.add_argument("--yes", action="store_true", help="confirm apply")
    p_evolve.set_defaults(func=cmd_evolve)

    sub.add_parser("verify", help="verify predictions").set_defaults(func=cmd_verify)

    p_review = sub.add_parser("review", help="Judge labelling")
    p_review.add_argument("--sample", default=None, help="sample id to label")
    p_review.add_argument("--label", type=float, default=None, help="human label 0..1 (required with --sample)")
    p_review.set_defaults(func=cmd_review)

    sub.add_parser("rollback", help="revert last applied change").set_defaults(func=cmd_rollback)
    sub.add_parser("status", help="overall status").set_defaults(func=cmd_status)
    sub.add_parser("serve", help="run the MCP server").set_defaults(func=cmd_serve)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The hook receiver is dispatched before argparse so its raw event arg passes through.
    if argv and argv[0] == "hook":
        from .hooks.record import main as hook_main

        return hook_main(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
