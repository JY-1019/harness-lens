"""Command-line interface.

    harness-lens install        wire into Claude Code + init runtime
    harness-lens skill          (re)install the SKILL wrapper (--print to show)
    harness-lens harness        inspect the harness applied to a project
    harness-lens enforce        write the 3-Layer criteria into the instruction file
    harness-lens layers         show the 3-Layer criteria currently enforced
    harness-lens show [--fail]  recent Flows (with 3-Layer view)
    harness-lens diagnose       Pillar 2 — Debugger agent
    harness-lens evolve         Pillar 3 — proposals (+ --apply ID --yes)
    harness-lens verify         verify predictions → confirm / roll back
    harness-lens review         Judge labelling
    harness-lens rollback       revert last applied change
    harness-lens status         3-Layer + prediction hit-rate + Judge
    harness-lens serve          run the MCP server
    harness-lens gui            launch the local web GUI (monitor + edit the 3-Layer harness)
    harness-lens benchmark      verify the managed harness honours its 3-Layer boundaries
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
    gap_count = flow.get("gap_count", 0)
    gap = f"   |  gap: {flow.get('gap_ratio', 0.0):.0%} (관측 불가 {gap_count} step)" if gap_count else ""
    l1_failed = flow.get("layer1_failed", 0)
    l1 = "ok" if not l1_failed else f"위반 {l1_failed}"
    triggers = flow.get("layer3_triggers", [])
    l3 = "ok" if not triggers else ", ".join(triggers)
    lines = [
        f"Flow {flow['session_id'][:8]}  [{flow['platform']}]  "
        f"tokens {flow['total_tokens']:,}  {mark}",
        f"  Layer 1: {l1}   Layer 2: {l2}   Layer 3: {l3}{gap}",
    ]
    for i, task in enumerate(flow["tasks"], 1):
        steps = task["steps"]
        retries = sum(s["retry_count"] for s in steps)
        fails = sum(1 for s in steps if s["success"] is False)
        unobserved = any(s.get("observed") is False for s in steps)
        # A gap step has no observed outcome, so it is neither pass nor fail — mark it "?"
        # rather than a misleading ✅ (design Codex §14).
        flag = "⚠" if fails else ("?" if unobserved else "✅")
        extra = f"  (retry {retries})" if retries else ""
        lines.append(f"  Task {i} [{task['category']}]  {flag}  {len(steps)} steps{extra}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_install(args) -> int:
    # --enforce/--observe install the control-plane daemon hooks (relay → daemon); without
    # either flag, install the legacy observe-only hooks (unchanged behaviour for upgraders).
    mode = "enforce" if args.enforce else ("observe" if args.observe else None)
    if mode is not None:
        from .daemon.install import install_daemon

        report = install_daemon(mode=mode, platform_name=args.platform)
        print(report.render())
        return 0

    from .hooks.install import install

    report = install(platform_name=args.platform)
    print(report.render())
    return 0


def cmd_daemon(args) -> int:
    from .daemon import runner

    action = args.daemon_action
    if action == "start":
        res = runner.start()
        print(f"daemon: {res.get('status')} (pid {res.get('pid')})")
        if res.get("detail"):
            print(f"  {res['detail']}")
        return 0 if res.get("status") in ("started", "already-running", "starting") else 1
    if action == "stop":
        print(f"daemon: {runner.stop().get('status')}")
        return 0
    res = runner.status()  # status
    if res.get("ok"):
        print(
            f"daemon: running (pid {res.get('pid')})  mode={res.get('mode')}  "
            f"pending={res.get('pending_approvals')}  rev={res.get('rev')}"
        )
        return 0
    print(f"daemon: 미응답 (pid {res.get('pid')}, running={res.get('running')})")
    return 1


def cmd_mode(args) -> int:
    import json
    import urllib.error
    import urllib.request

    from .daemon import daemon_base_url
    from .daemon.config import DaemonConfig, ensure_token

    token = ensure_token()
    req = urllib.request.Request(
        f"{daemon_base_url()}/api/mode",
        data=json.dumps({"mode": args.mode}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-HL-Token": token}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        print(f"mode → {body.get('mode')} (실행 중 데몬에 적용됨)")
        return 0
    except (urllib.error.URLError, OSError):
        cfg = DaemonConfig.load()
        cfg.mode = args.mode
        cfg.save()
        print(f"mode → {args.mode} (데몬 미기동 — 다음 start 시 적용)")
        return 0


def cmd_approvals(args) -> int:
    import json
    import urllib.error
    import urllib.request

    from .daemon import daemon_base_url
    from .daemon.config import ensure_token

    token = ensure_token()
    base = daemon_base_url()
    try:
        req = urllib.request.Request(f"{base}/api/approvals", headers={"X-HL-Token": token})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            pending = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError):
        print("데몬에 연결할 수 없습니다 (harness-lens daemon start).", file=sys.stderr)
        return 1
    if not pending:
        print("대기 중인 승인이 없습니다.")
        return 0
    for a in pending:
        print(f"\n승인 대기: {a['approval_id']}  step={a['step_id']}")
        ans = input("  승인하시겠습니까? [y/N] ").strip().lower()
        resolution = "approved" if ans == "y" else "denied"
        reason = input("  거부 사유(선택): ").strip() or None if resolution == "denied" else None
        body = json.dumps({"resolution": resolution, "reason": reason}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/api/approvals/{a['approval_id']}", data=body,
            headers={"Content-Type": "application/json", "X-HL-Token": token}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0):
                pass
            print(f"  → {resolution}")
        except (urllib.error.URLError, OSError) as exc:
            print(f"  실패: {exc}", file=sys.stderr)
    return 0


def cmd_tail(args) -> int:
    import time

    from . import home_dir
    from .daemon.ledger import DaemonLedger

    ledger = DaemonLedger(home_dir() / "ledger.db")
    after = 0.0 if args.all else time.time()
    try:
        while True:
            for e in ledger.events_since(after):
                after = max(after, e["ts"])
                flow = (e["flow_id"] or "")[:8]
                print(f"{e['ts']:.3f}  {e['kind']:18}  flow={flow}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        ledger.close()


def cmd_skill(args) -> int:
    from .detector import detect
    from .hooks.install import _cli_invocation
    from .skill import install_skill, render

    platform = detect(args.platform)
    if platform is None:
        print(
            "지원되는 하네스를 찾지 못했습니다 (Claude Code / Codex 미설치). "
            "--platform 으로 지정하거나 해당 하네스를 설치하세요.",
            file=sys.stderr,
        )
        return 1
    invoke = _cli_invocation()
    if args.print:
        print(render(platform, invoke=invoke))
        return 0
    path, changed = install_skill(platform, invoke=invoke)
    print(f"SKILL {'작성됨' if changed else '변경 없음'} ({platform.label}): {path}")
    return 0


def cmd_show(args) -> int:
    # Prefer the daemon's Flow/Task/Step ledger when present, so CLI and GUI read the *same*
    # tree and never disagree on numbers (design constraint). Fall back to the legacy store for
    # an observe-only install that never adopted the daemon.
    from . import home_dir

    db = home_dir() / "ledger.db"
    if _has_daemon_schema(db):
        return _show_daemon(db, args)

    if getattr(args, "flow", None):
        print("daemon ledger가 없어 특정 Flow 조회를 지원하지 않습니다 (harness-lens install --observe 후 사용).", file=sys.stderr)
        return 1
    service = _service()
    flows = service.get_flow_summary(limit=args.limit, only_failed=args.fail)
    if not flows:
        print("기록된 Flow가 없습니다. Claude Code에서 작업하면 자동 추적됩니다.")
        return 0
    print("\n\n".join(_render_flow(f) for f in flows))
    return 0


def _has_daemon_schema(db_path) -> bool:
    """Whether ``ledger.db`` carries the daemon's new Flow/Task/Step schema (a ``flows`` table)."""
    import sqlite3

    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='flows'"
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _show_daemon(db_path, args) -> int:
    from .daemon.ledger import DaemonLedger

    ledger = DaemonLedger(db_path)
    try:
        if getattr(args, "flow", None):
            tree = ledger.flow_tree(args.flow)
            if tree is None:
                print(f"Flow를 찾을 수 없습니다: {args.flow}", file=sys.stderr)
                return 1
            print(_render_daemon_flow(tree))
            return 0
        flows = ledger.list_flows(limit=args.limit, status="failed" if args.fail else None)
        if not flows:
            print("기록된 Flow가 없습니다. 데몬 설치 후 작업하면 자동 추적됩니다.")
            return 0
        trees = [t for t in (ledger.flow_tree(f.flow_id) for f in flows) if t]
        print("\n\n".join(_render_daemon_flow(t) for t in trees))
        return 0
    finally:
        ledger.close()


_DAEMON_MARK = {"completed": "✅", "failed": "⚠", "running": "…", "aborted": "⛔"}


def _render_daemon_flow(tree: dict) -> str:
    mark = _DAEMON_MARK.get(tree["status"], "?")
    lines = [
        f"Flow {tree['flow_id'][:8]}  [{tree['source']}]  "
        f"tokens {tree.get('total_tokens', 0):,}  {mark}   mode={tree.get('mode', '?')}"
    ]
    if tree.get("title"):
        lines.append(f"  {tree['title']}")

    def walk(tasks, depth):
        indent = "  " * (depth + 1)
        for task in tasks:
            steps = task.get("steps", [])
            fails = sum(1 for s in steps if s.get("status") == "failed")
            flag = "⚠" if fails else ("…" if task.get("status") == "running" else "✅")
            if task.get("kind") == "subagent":
                label = "🤖 " + (task.get("agent_name") or "subagent")
            else:
                label = task.get("title") or "turn"
            extra = f"  (retry {task['retry_count']})" if task.get("retry_count") else ""
            lines.append(f"{indent}Task [{label[:40]}]  {flag}  {len(steps)} steps{extra}")
            for s in steps:
                dec = ""
                if s.get("decision") and s["decision"] != "allow":
                    dec = f"  L{s.get('decision_layer')}:{s['decision']}"
                sc = f"  L2 {s['judge_score']:.2f}" if s.get("judge_score") is not None else ""
                lines.append(f"{indent}  - {s.get('tool_name', '?')}  {s.get('status')}{dec}{sc}")
            walk(task.get("children", []), depth + 1)

    walk(tree.get("tasks", []), 0)
    return "\n".join(lines)


def cmd_harness(args) -> int:
    from pathlib import Path

    from .harness import LensUnsupportedPlatform

    service = _service()
    try:
        report = service.inspect_project_harness(
            Path(args.project) if args.project else None,
            platform_name=args.platform,
        )
    except LensUnsupportedPlatform as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(report.render())
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
    print(f"  gap 비율    : {s.get('gap_ratio', 0.0):.0%}")
    print(f"  Layer 1    : invariant {len(s['layer1'])}개")
    print(f"  Layer 2    : domain criterion {len(s['layer2'])}개")
    print(f"  Layer 3    : {s['layer3']}")
    print(f"  수정안      : {s['candidates']}")
    if not service.llm_available():
        print("  (참고) LLM 백엔드 없음 — ANTHROPIC_API_KEY 설정 또는 claude/codex CLI 로그인 필요 (diagnose/evolve 비활성)")
    return 0


def cmd_enforce(args) -> int:
    from .criteria.layer import CriteriaViolation

    service = _service()
    try:
        target = service.enforce_criteria(platform_name=args.platform)
    except CriteriaViolation as exc:
        print(f"강제 거부: {exc}", file=sys.stderr)
        return 1
    if target is None:
        print("지원되는 하네스를 찾지 못했습니다 (Claude Code / Codex 미설치).", file=sys.stderr)
        return 1
    print(f"3-Layer 하네스를 강제했습니다 → {target}")
    return 0


def cmd_layers(args) -> int:
    service = _service()
    v = service.layers_view()
    print("harness-lens 3-Layer 하네스 (현재 강제 중)")
    print("  Layer 1 — Invariants (절대 위반 금지):")
    for rule in v["invariants"]:
        print(f"    - {rule}")
    print("  Layer 2 — Domain criteria (행동 기준):")
    for dc in v["domain_criteria"]:
        print(f"    - [{dc['id']}] {dc['description']} (weight {dc['weight']})")
    print("  Layer 3 — QA thresholds (품질 한계선):")
    for key, value in v["layer3"].items():
        print(f"    - {key}: {value}")
    scopes = v.get("scopes", [])
    if scopes:
        print("  Scopes — 프로젝트/세션별 정책 (전역 base 위에 덮어씀):")
        for s in scopes:
            match = s["cwd_prefix"] and f"cwd~{s['cwd_prefix']}" or (s["session_id"] and f"session={s['session_id']}") or "?"
            extras = []
            if s["mode"]:
                extras.append(f"mode={s['mode']}")
            if s["layer3"]:
                extras.append("layer3=" + ", ".join(f"{k}:{val}" for k, val in s["layer3"].items()))
            if s["add_invariants"]:
                extras.append(f"+{len(s['add_invariants'])} invariant")
            print(f"    - [{s['name']}] {match}  ({'; '.join(extras) or '변경 없음'})")
    return 0


def cmd_serve(args) -> int:
    from .server import main as serve_main

    return serve_main()


def cmd_gui(args) -> int:
    # Prefer the live daemon GUI (/ui) when the daemon is up — it renders the same Flow/Task/Step
    # tree the CLI reads, with live updates and approvals. Fall back to the legacy localhost
    # dashboard (observe-only data) when no daemon is running.
    from .daemon import daemon_base_url, runner

    if runner.status().get("ok"):
        url = f"{daemon_base_url()}/ui"
        print(f"daemon GUI → {url}")
        if not args.no_browser:
            import webbrowser

            try:
                webbrowser.open(url)
            except Exception:
                pass
        return 0

    from .gui import serve

    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_benchmark(args) -> int:
    from . import home_dir
    from .benchmark import run_benchmark
    from .criteria import ThreeLayerCriteria

    # Load criteria.yaml directly — the benchmark is read-only and meant to run in isolated/CI
    # environments, so it must not open or migrate the ledger (LensService) as a side effect.
    criteria = ThreeLayerCriteria.load(home_dir() / "criteria.yaml")
    report = run_benchmark(criteria)
    print(report.render())
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness-lens", description="Observe and evolve agentic harnesses.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="wire into Claude Code / Codex + init runtime")
    p_install.add_argument("--platform", default=None, help="force a platform id (default: auto-detect)")
    # Control-plane daemon install (relay hooks + starting mode). Without either flag, the legacy
    # observe-only hooks are installed.
    p_install.add_argument("--enforce", action="store_true", help="install daemon hooks in enforce mode")
    p_install.add_argument("--observe", action="store_true", help="install daemon hooks in observe mode")
    p_install.set_defaults(func=cmd_install)

    p_daemon = sub.add_parser("daemon", help="run/stop/inspect the control-plane daemon")
    p_daemon.add_argument("daemon_action", choices=["start", "stop", "status"],
                          help="start, stop, or check the control-plane daemon")
    p_daemon.set_defaults(func=cmd_daemon)

    p_mode = sub.add_parser("mode", help="switch the daemon between observe/enforce at runtime")
    p_mode.add_argument("mode", choices=["observe", "enforce"],
                        help="observe (record only) or enforce (deny/escalate)")
    p_mode.set_defaults(func=cmd_mode)

    sub.add_parser("approvals", help="resolve pending escalations from the terminal").set_defaults(func=cmd_approvals)

    p_tail = sub.add_parser("tail", help="stream daemon HarnessEvents")
    p_tail.add_argument("--all", action="store_true", help="include events from before now")
    p_tail.set_defaults(func=cmd_tail)

    p_skill = sub.add_parser("skill", help="(re)install the SKILL wrapper for the host harness")
    p_skill.add_argument("--platform", default=None, help="force a platform id (default: auto-detect)")
    p_skill.add_argument("--print", action="store_true", help="print the skill instead of writing it")
    p_skill.set_defaults(func=cmd_skill)

    p_show = sub.add_parser("show", help="recent Flows (or a single Flow tree)")
    p_show.add_argument("flow", nargs="?", default=None, help="a flow_id to show in full (daemon ledger)")
    p_show.add_argument("--fail", action="store_true", help="only failed Flows")
    p_show.add_argument("--limit", type=int, default=20, help="max Flows to list (default: 20)")
    p_show.set_defaults(func=cmd_show)

    p_harness = sub.add_parser("harness", help="inspect the harness applied to a project")
    p_harness.add_argument("--project", default=None, help="project root (default: cwd)")
    p_harness.add_argument("--platform", default=None, help="force a platform id (default: auto-detect)")
    p_harness.set_defaults(func=cmd_harness)

    p_enforce = sub.add_parser("enforce", help="write the 3-Layer criteria into the instruction file")
    p_enforce.add_argument("--platform", default=None, help="force a platform id (default: auto-detect)")
    p_enforce.set_defaults(func=cmd_enforce)

    sub.add_parser("layers", help="show the 3-Layer criteria currently enforced").set_defaults(func=cmd_layers)

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

    p_gui = sub.add_parser("gui", help="launch the local web GUI (monitor + edit the 3-Layer harness)")
    p_gui.add_argument("--port", type=int, default=8765,
                       help="legacy-GUI port (default: 8765); when the daemon is up, opens its :7700 /ui instead")
    p_gui.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    p_gui.set_defaults(func=cmd_gui)

    sub.add_parser(
        "benchmark", help="verify the managed harness honours its 3-Layer boundaries"
    ).set_defaults(func=cmd_benchmark)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The hook receiver is dispatched before argparse so its raw event arg passes through.
    if argv and argv[0] == "hook":
        from .hooks.record import main as hook_main

        return hook_main(argv[1:])
    # The daemon relay is dispatched before argparse too: hooks invoke it with a raw source arg
    # and pipe the event payload on stdin.
    if argv and argv[0] == "hook-relay":
        from .daemon.client import main as relay_main

        return relay_main(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
