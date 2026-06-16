"""DaemonRuntime — the harness-agnostic core that turns a hook payload into a response.

This is deliberately transport-free so it can be exercised without HTTP: the FastAPI app
(:mod:`harness_lens.daemon.app`) is a thin shell over :meth:`DaemonRuntime.handle`. The
runtime owns the ledger, policy engine, approval queue, event bus and the single writer,
and reconstructs the Flow/Task/Step tree from the normalized event stream.

Reconstruction state is kept in memory (``_flows``) keyed by flow_id — the daemon is one
long-lived process handling N concurrent sessions, so a per-flow cursor lives here rather
than in a persisted cursor table. The ledger is the durable mirror.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .. import home_dir
from ..components import ComponentManager
from ..criteria import (
    DEFAULT_CRITERIA_YAML, ThreeLayerCriteria, apply_scope, load_scopes, parse_scopes,
    resolve_scope, scope_to_payload,
)
from .adapters import get_adapter
from .approvals import APPROVED, DENIED, TIMEOUT, ApprovalQueue
from .bus import EventBus
from .capabilities import ALLOW, DENY, ESCALATE, Decision
from .config import (
    MODE_ENFORCE,
    TIMEOUT_ALLOW,
    TIMEOUT_DENY,
    TIMEOUT_ESCALATE_TERMINAL,
    DaemonConfig,
    ensure_token,
)
from .events import HarnessEvent, mask_secrets
from .ledger import DaemonLedger, Flow, Step, Task, migrate_legacy, relocate_legacy_db
from .policy import PolicyContext, PolicyEngine, looks_like_test, read_path_of
from .writer import LedgerWriter


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Upper bound on the stored user-prompt text (the turn task title). Large enough to keep the full
# request for display, small enough that a pathological paste cannot bloat the ledger row.
_PROMPT_MAX = 4000


@dataclass
class FlowState:
    """In-memory reconstruction cursor for one Flow."""

    flow_id: str
    cwd: Optional[str] = None  # the project dir — selects the per-scope policy for this Flow
    current_turn_task: Optional[str] = None
    subagent_stack: list[str] = field(default_factory=list)  # nested subagent task ids
    pending_title: Optional[str] = None
    read_paths: set[str] = field(default_factory=set)
    open_steps: dict[str, str] = field(default_factory=dict)  # pair-key → step_id
    failed_steps: int = 0
    total_steps: int = 0
    ran_tests: bool = False  # a test suite ran in this flow (for the L2 test-before-change check)

    @property
    def active_task(self) -> Optional[str]:
        # Steps attach to the innermost open subagent, else the current turn task.
        return self.subagent_stack[-1] if self.subagent_stack else self.current_turn_task


class DaemonRuntime:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or home_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = ensure_token(self.root)
        self.config = DaemonConfig.load(self.root)
        # The daemon owns ledger.db (new schema); relocate any legacy observe-only DB aside first.
        relocate_legacy_db(self.root)
        self.ledger = DaemonLedger(self.root / "ledger.db")
        self.criteria_path = self.root / "criteria.yaml"
        self.criteria = ThreeLayerCriteria.load(self.criteria_path)
        self.policy = PolicyEngine(self.criteria, self.criteria_path)
        # Per-project/session scopes layered over the global base. Each distinct scope gets its own
        # PolicyEngine, built lazily and cached by scope name; the global engine above is the
        # fallback for sessions that match no scope.
        self.scopes = load_scopes(self.criteria_path)
        self._scope_policies: dict[str, PolicyEngine] = {}
        self.approvals = ApprovalQueue()
        self.bus = EventBus()
        self.writer = LedgerWriter()
        self._flows: dict[str, FlowState] = {}
        self._tail = None
        self._loop = None
        self._started = False

    # -- lifecycle ------------------------------------------------------- #
    async def start(self) -> None:
        if self._started:
            return
        self.writer.start()
        self._loop = asyncio.get_event_loop()
        # Import historical observe-only Flows so they are viewable, then clear any approval left
        # pending by a previous daemon (its blocked hook processes are gone).
        await self.writer.submit(lambda: migrate_legacy(self.ledger))
        await self.writer.submit(lambda: self.ledger.deny_all_pending("daemon restart"))
        self._start_tail()
        self._started = True

    def _start_tail(self) -> None:
        """Start the auxiliary JSONL tail (best-effort; never fatal to the daemon)."""
        try:
            from .tail import JsonlTail, enrich_tokens
        except Exception:  # noqa: BLE001
            return

        def on_record(source: str, record: dict) -> None:
            result = enrich_tokens(record)
            if result is None:
                return
            session_id, total = result
            # Only enrich a Flow we already know from hooks (tail is augment-only, not a source).
            if self._loop is not None and self.ledger.get_flow(session_id) is not None:
                self._loop.call_soon_threadsafe(
                    lambda: self.writer.enqueue(lambda: self.ledger.set_flow_tokens(session_id, total))
                )

        try:
            self._tail = JsonlTail(on_record)
            self._tail.start()
        except Exception:  # noqa: BLE001
            self._tail = None

    async def stop(self) -> None:
        if self._tail is not None:
            self._tail.stop()
            self._tail = None
        self.approvals.fail_all(DENIED)
        await self.writer.stop()
        self.ledger.close()
        self._started = False

    def set_mode(self, mode: str) -> str:
        self.config.mode = mode
        self.config.save(self.root)
        self.bus.publish("mode_changed", data={"mode": self.config.mode})
        return self.config.mode

    def reload_criteria(self) -> None:
        self.criteria = ThreeLayerCriteria.load(self.criteria_path)
        self.policy = PolicyEngine(self.criteria, self.criteria_path)
        self.scopes = load_scopes(self.criteria_path)
        self._scope_policies.clear()  # rebuilt lazily against the reloaded base/scopes

    # -- scope editing (GUI/API) ----------------------------------------- #
    def scopes_payload(self) -> list[dict]:
        """Current scopes as plain dicts for the API/GUI."""
        return [scope_to_payload(s) for s in self.scopes]

    def save_scopes(self, raw_scopes: list) -> list[dict]:
        """Validate + persist the scopes section of criteria.yaml (backed up), then hot-reload.

        Only the ``scopes:`` key is rewritten; the global base layers are preserved. Returns the
        cleaned scopes now in force. (PyYAML does not preserve comments, so the commented example
        in a pristine criteria.yaml is dropped once scopes are saved from the GUI — by design, the
        file becomes GUI-managed from then on.)"""
        cleaned = parse_scopes(raw_scopes)
        # Start from the current file so the global base layers are preserved; if there is no file
        # yet, seed from the default so saving scopes never silently drops the base invariants.
        source = (
            self.criteria_path.read_text(encoding="utf-8")
            if self.criteria_path.exists() else DEFAULT_CRITERIA_YAML
        )
        try:
            loaded = yaml.safe_load(source)
        except yaml.YAMLError:
            loaded = None
        data: dict = loaded if isinstance(loaded, dict) else {}
        if cleaned:
            data["scopes"] = [scope_to_payload(s) for s in cleaned]
        else:
            data.pop("scopes", None)
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        # criteria.yaml is the qa.py-managed component, so reuse that name for the same backup
        # machinery the legacy Layer-3 edit path uses.
        ComponentManager(self.root).apply("qa.py", self.criteria_path, text)
        self.reload_criteria()
        self.bus.publish("criteria_changed", data=self.criteria_payload())
        return self.scopes_payload()

    # -- base 3-Layer editing (GUI/API) ---------------------------------- #
    def criteria_payload(self) -> dict:
        """The global base 3-Layer criteria as plain data for the harness editor."""
        return self._criteria_payload(self.criteria)

    @staticmethod
    def _criteria_payload(criteria: ThreeLayerCriteria) -> dict:
        """A serialisable view of a 3-Layer criteria object."""
        return {
            "invariants": list(criteria.invariants),
            "domain_criteria": [
                {"id": dc.id, "description": dc.description, "weight": dc.weight}
                for dc in criteria.domain_criteria
            ],
            "layer3": criteria.qa.config.as_dict(),
        }

    def effective_criteria_payload(
        self, cwd: Optional[str] = None, session_id: Optional[str] = None,
        flow_id: Optional[str] = None,
    ) -> dict:
        """The effective 3-Layer harness for a project/session after scope resolution."""
        if flow_id:
            flow = self.ledger.get_flow(flow_id)
            if flow is not None:
                cwd = cwd or flow.cwd
                session_id = session_id or flow.flow_id
        scope = resolve_scope(self.scopes, cwd, session_id)
        effective = apply_scope(self.criteria, scope)
        out = self._criteria_payload(effective)
        out["scope"] = scope_to_payload(scope) if scope else None
        out["scope_name"] = scope.name if scope else "global"
        out["mode"] = (scope.mode or self.config.mode) if scope else self.config.mode
        out["cwd"] = cwd
        out["session_id"] = session_id
        return out

    def harness_summary(self, cwd: Optional[str], session_id: Optional[str]) -> dict:
        """Compact Flow-card summary of the harness currently selected for a Flow."""
        effective = self.effective_criteria_payload(cwd=cwd, session_id=session_id)
        scope = effective.get("scope") or {}
        return {
            "scope_name": effective["scope_name"],
            "scope": effective.get("scope"),
            "mode": effective["mode"],
            "layer3": effective["layer3"],
            "invariants_count": len(effective["invariants"]),
            "domain_criteria_count": len(effective["domain_criteria"]),
            "added_invariants_count": len(scope.get("add_invariants") or []),
            "added_domain_criteria_count": len(scope.get("add_domain_criteria") or []),
        }

    def flow_payload(self, flow: Flow) -> dict:
        """A Flow row enriched with the project/session harness that applies to it."""
        data = _row(flow)
        data["harness"] = self.harness_summary(flow.cwd, flow.flow_id)
        return data

    def flow_tree_payload(self, flow_id: str) -> Optional[dict]:
        """Tree read model with the 3-Layer summary + per-step service-harness attribution."""
        tree = self.ledger.flow_tree(flow_id)
        if tree is None:
            return None
        tree["harness"] = self.harness_summary(tree.get("cwd"), tree.get("flow_id"))
        self._annotate_usage(tree.get("tasks") or [])
        tree["l3_status"] = _l3_status(tree.get("tasks") or [], (tree.get("harness") or {}).get("layer3") or {})
        return tree

    def _annotate_usage(self, tasks: list) -> None:
        """Attach ``harness_usage`` to every step so the trajectory shows which scaffolding
        (skill/command/workflow/mcp/instruction/cursor rule) each action exercised — recovering
        signal lost when, e.g., Codex runs almost everything through one ``Bash``/shell tool."""
        for task in tasks:
            for step in task.get("steps") or []:
                step["harness_usage"] = self.usage_for_step(
                    step.get("tool_name"), step.get("tool_input"), step.get("tool_output")
                )
            self._annotate_usage(task.get("children") or [])

    @staticmethod
    def usage_for_step(tool_name: Optional[str], tool_input, tool_output) -> list[dict]:
        """Best-effort attribution of the service harness a single step exercised (read-time)."""
        from ..harness_usage import detect_usage

        if not tool_name:
            return []
        usages = detect_usage(tool_name, _as_text(tool_input), _as_text(tool_output))
        return [{"kind": u.kind, "name": u.name} for u in usages]

    def service_harness_payload(self, flow_id: str) -> Optional[dict]:
        """The external scaffolding (CLAUDE.md/AGENTS.md/skills/workflows/settings/MCP/hooks +
        .cursor/rules) applied to a Flow's project — distinct from the 3-Layer harness."""
        flow = self.ledger.get_flow(flow_id)
        if flow is None:
            return None
        from ..detector import detect
        from ..harness import LensUnsupportedPlatform, inspect_project

        platform_name = "claude-code" if flow.source == "claude_code" else "codex"
        platform = detect(platform_name)
        components: list[dict] = []
        tool_categories: dict = {}
        if platform is not None and flow.cwd:
            try:
                report = inspect_project(Path(flow.cwd), platform, self.criteria)
                components = [
                    {"component": c.component, "kind": c.kind, "scope": c.scope,
                     "path": str(c.path), "exists": c.exists, "editable": c.editable,
                     "detail": c.detail}
                    for c in report.applied()
                ]
                tool_categories = report.tool_categories
            except LensUnsupportedPlatform:
                pass
        return {
            "flow_id": flow_id, "source": flow.source, "cwd": flow.cwd,
            "platform": platform.label if platform else None,
            "components": components + _scan_cursor_rules(flow.cwd),
            "tool_categories": tool_categories,
        }

    def component_prompt(self, kind: str, name: str, cwd: Optional[str] = None) -> dict:
        """Resolve a service-harness component (skill/command/workflow/instruction/cursor rule)
        to its on-disk prompt file and return its text — so the GUI can show *what that scaffolding
        actually told the agent* when its chip is clicked in the trajectory."""
        path = _resolve_component_path(kind, name, cwd)
        if path is None:
            note = ("MCP 서버 설정 — 프롬프트 파일 없음" if kind in ("mcp", "mcp_config")
                    else "프롬프트 파일을 찾지 못했습니다 (전역/프로젝트 양쪽 확인)")
            return {"kind": kind, "name": name, "found": False, "path": None, "content": None, "note": note}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"kind": kind, "name": name, "found": False, "path": str(path),
                    "content": None, "note": str(exc)}
        limit = 16000
        truncated = len(text) > limit
        return {"kind": kind, "name": name, "found": True, "path": str(path),
                "content": text[:limit] + ("\n\n…(생략됨)" if truncated else ""), "truncated": truncated}

    def edit_criteria(self, layer: str, payload: dict) -> dict:
        """Apply a human owner edit to one base layer, then hot-reload the live policy.

        Reuses :class:`LensService`'s edit methods so the daemon and the (fallback) CLI/GUI share
        one set of validation + backup + instruction-block re-enforcement. The autonomous evolver
        is still pinned to Layer 3 by the CriteriaGuard; this is the human's explicit edit path.
        ``layer3`` also flows through here so the editor has one consistent endpoint per layer.
        """
        from ..service import LensService

        svc = LensService(root=self.root)
        try:
            if layer == "layer1":
                svc.update_invariants(payload.get("invariants", []))
            elif layer == "layer2":
                svc.update_domain_criteria(payload.get("domain_criteria", []))
            elif layer == "layer3":
                svc.update_layer3(payload.get("layer3", payload))
            else:
                raise ValueError(f"unknown layer {layer!r} (expected layer1|layer2|layer3)")
        finally:
            svc.close()
        # LensService wrote criteria.yaml; refresh the daemon's in-memory criteria + scope policies
        # so the very next hook is judged against the new harness, and tell any open GUI to reload.
        self.reload_criteria()
        self.bus.publish("criteria_changed", data=self.criteria_payload())
        return self.criteria_payload()

    def _scope_for(self, event: HarnessEvent):
        """The scope (if any) that applies to this event's project/session."""
        cwd = event.cwd
        if cwd is None:
            st = self._flows.get(event.session_id)
            cwd = st.cwd if st else None
        return resolve_scope(self.scopes, cwd, event.session_id)

    def _effective_mode(self, event: HarnessEvent) -> str:
        """The mode in force for this Flow — its scope's pin, else the global daemon mode."""
        scope = self._scope_for(event)
        return (scope.mode or self.config.mode) if scope else self.config.mode

    def _policy_for(self, event: HarnessEvent) -> tuple["PolicyEngine", str]:
        """Resolve the effective (engine, mode) for this event — its scope's, else the global base."""
        scope = self._scope_for(event)
        if scope is None:
            return self.policy, self.config.mode
        engine = self._scope_policies.get(scope.name)
        if engine is None:
            engine = PolicyEngine(apply_scope(self.criteria, scope), self.criteria_path)
            self._scope_policies[scope.name] = engine
        return engine, (scope.mode or self.config.mode)

    # -- main entry ------------------------------------------------------ #
    async def handle(self, source: str, payload: dict) -> dict:
        adapter = get_adapter(source)
        if adapter is None:
            return {}
        event = adapter.to_event(payload)
        if event is None:
            return {}  # unknown/ignored hook event — never block

        # Reconstruct the tree (and durably append the raw event) on the single writer.
        step_id = await self.writer.submit(lambda: self._ingest(event))

        if not event.is_control:
            return {}
        decision = await self._decide(event, step_id)
        return adapter.render(decision, event)

    # -- tree reconstruction (runs on the writer coroutine) -------------- #
    def _ingest(self, event: HarnessEvent) -> Optional[str]:
        flow_id = event.session_id
        self.ledger.add_event(event.event_id, flow_id, event.kind, event.ts, event.raw)
        handler = {
            "session_start": self._on_session_start,
            "user_prompt": self._on_user_prompt,
            "subagent_start": self._on_subagent_start,
            "subagent_stop": self._on_subagent_stop,
            "pre_tool_use": self._on_pre_tool,
            "post_tool_use": self._on_post_tool,
            "post_tool_failure": self._on_post_tool,
            "stop": self._on_stop,
            "session_end": self._on_session_end,
        }.get(event.kind)
        return handler(event) if handler else None

    def _state(self, event: HarnessEvent) -> FlowState:
        st = self._flows.get(event.session_id)
        if st is None:
            st = FlowState(flow_id=event.session_id, cwd=event.cwd)
            # Daemon restarted mid-session: in-memory state was lost. Resume the Flow's open turn
            # from the ledger so the next tool reuses it (and its title) instead of opening a
            # titleless turn — the cause of "(요청 미관측)" turns proliferating across restarts.
            if self.ledger.get_flow(event.session_id) is not None:
                st.current_turn_task = self.ledger.latest_running_turn(event.session_id)
                if st.cwd is None:
                    flow = self.ledger.get_flow(event.session_id)
                    st.cwd = flow.cwd if flow else None
            self._flows[event.session_id] = st
        elif st.cwd is None and event.cwd:
            st.cwd = event.cwd
            # A control event can arrive before we saw session_start (hook ordering / daemon
            # restart mid-session); make sure a Flow row exists so steps have a parent.
            if self.ledger.get_flow(event.session_id) is None:
                self._publish_flow(self._new_flow(event))
        return st

    def _new_flow(self, event: HarnessEvent) -> Flow:
        return Flow(
            flow_id=event.session_id, source=event.source, status="running",
            mode=self._effective_mode(event), started_at=event.ts, cwd=event.cwd, model=event.model,
        )

    def _on_session_start(self, event: HarnessEvent) -> None:
        self._flows[event.session_id] = FlowState(flow_id=event.session_id, cwd=event.cwd)
        flow = self.ledger.get_flow(event.session_id) or self._new_flow(event)
        flow.source, flow.mode, flow.cwd, flow.model = (
            event.source, self._effective_mode(event), event.cwd or flow.cwd, event.model or flow.model
        )
        flow.status = "running"
        self._publish_flow(flow)

    def _on_user_prompt(self, event: HarnessEvent) -> None:
        st = self._state(event)
        # Keep the full prompt as the turn task's title so the GUI/CLI can show the exact request
        # (a generous cap only guards against a pathologically large paste). The flow keeps a short
        # one-line label for the sidebar/header — the full text lives on the task.
        prompt = (event.prompt or "").strip()
        st.pending_title = prompt[:_PROMPT_MAX] or st.pending_title
        st.current_turn_task = None  # next tool opens a fresh turn task
        flow = self.ledger.get_flow(event.session_id)
        if flow is not None and not flow.title and prompt:
            flow.title = prompt.splitlines()[0][:80]
            self._publish_flow(flow)

    def _on_subagent_start(self, event: HarnessEvent) -> None:
        st = self._state(event)
        task = Task(
            task_id=_uid("task"), flow_id=event.session_id, kind="subagent",
            parent_task_id=st.active_task, agent_name=event.agent_name,
            title=event.agent_name or "subagent", status="running",
            started_at=event.ts, seq=self.ledger.next_task_seq(event.session_id),
        )
        self._publish_task(task)
        st.subagent_stack.append(task.task_id)

    def _on_subagent_stop(self, event: HarnessEvent) -> None:
        st = self._state(event)
        if st.subagent_stack:
            task_id = st.subagent_stack.pop()
            task = self.ledger.get_task(task_id)
            if task is not None:
                task.status = "completed"
                task.ended_at = event.ts
                self._publish_task(task)

    def _ensure_turn_task(self, event: HarnessEvent, st: FlowState) -> str:
        if st.active_task is not None:
            return st.active_task
        task = Task(
            task_id=_uid("task"), flow_id=event.session_id, kind="turn",
            title=st.pending_title, status="running", started_at=event.ts,
            seq=self.ledger.next_task_seq(event.session_id),
        )
        self._publish_task(task)
        st.current_turn_task = task.task_id
        return task.task_id

    def _on_pre_tool(self, event: HarnessEvent) -> str:
        st = self._state(event)
        task_id = self._ensure_turn_task(event, st)
        step_id = event.tool_use_id or _uid("step")
        step = Step(
            step_id=step_id, task_id=task_id, flow_id=event.session_id,
            tool_name=event.tool_name or "?", status="running",
            tool_input=_json(mask_secrets(event.tool_input)) if event.tool_input else None,
            started_at=event.ts, seq=self.ledger.next_step_seq(task_id),
        )
        self._publish_step(step)
        st.open_steps[self._pair_key(event)] = step_id
        st.total_steps += 1
        rp = read_path_of(event)
        if rp:
            st.read_paths.add(rp)
        if looks_like_test(event.tool_text()):  # remember tests ran → L2 test-before-change check
            st.ran_tests = True
        return step_id

    def _on_post_tool(self, event: HarnessEvent) -> Optional[str]:
        st = self._state(event)
        key = self._pair_key(event)
        step_id = st.open_steps.pop(key, None) or event.tool_use_id
        step = self.ledger.get_step(step_id) if step_id else None
        if step is None:
            # PostToolUse with no matching pre (missed/timed-out pre-hook): synthesize the step.
            step_id = self._on_pre_tool(event)
            st.open_steps.pop(self._pair_key(event), None)
            step = self.ledger.get_step(step_id)
        if step is None:
            return None
        failed = event.kind == "post_tool_failure" or _is_failure(event)
        step.status = "failed" if failed else "ok"
        step.ended_at = event.ts
        if step.started_at:
            step.duration_ms = max(0, int((event.ts - step.started_at) * 1000))
        if event.tool_output is not None:
            step.tool_output = _json(mask_secrets(event.tool_output))
        if event.total_tokens:
            step.tokens = event.total_tokens
        self._publish_step(step)
        if failed:
            st.failed_steps += 1
        return step.step_id

    def _on_stop(self, event: HarnessEvent) -> None:
        flow = self.ledger.get_flow(event.session_id)
        if flow is None:
            return
        st = self._flows.get(event.session_id)
        flow.ended_at = event.ts
        flow.status = "failed" if (st and st.failed_steps) else "completed"
        self._publish_flow(flow)

    def _on_session_end(self, event: HarnessEvent) -> None:
        flow = self.ledger.get_flow(event.session_id)
        if flow is None:
            return
        flow.ended_at = event.ts
        if event.total_tokens:
            flow.total_tokens = event.total_tokens
        st = self._flows.get(event.session_id)
        flow.status = event.status or ("failed" if (st and st.failed_steps) else "completed")
        self._publish_flow(flow)
        self._flows.pop(event.session_id, None)

    @staticmethod
    def _pair_key(event: HarnessEvent) -> str:
        # tool_use_id pairs pre/post exactly; without it, fall back to the tool name (best effort).
        return event.tool_use_id or f"name:{event.tool_name}"

    # -- decision orchestration ------------------------------------------ #
    async def _decide(self, event: HarnessEvent, step_id: Optional[str]) -> Decision:
        if event.kind == "user_prompt":
            return Decision.allow()
        # Resolve the effective policy + mode for this Flow's project/session scope (global base if
        # it matches none), so a scoped project can enforce while the rest of the system observes.
        engine, mode = self._policy_for(event)
        if event.kind in ("stop", "subagent_stop"):
            return engine.evaluate_stop(event, mode, self._context(event))
        if event.kind != "pre_tool_use":
            return Decision.allow()

        decision = engine.evaluate_pre_tool(event, mode, self._context(event))
        if decision.action == ESCALATE and step_id is not None:
            decision = await self._await_approval(event, step_id, decision)
        if step_id is not None:
            await self.writer.submit(lambda: self._apply_decision(step_id, decision))
        return decision

    def _context(self, event: HarnessEvent) -> PolicyContext:
        st = self._flows.get(event.session_id) or FlowState(flow_id=event.session_id)
        return PolicyContext(
            read_paths=set(st.read_paths), failed_steps=st.failed_steps,
            total_steps=st.total_steps, ran_tests=st.ran_tests,
        )

    async def _await_approval(self, event: HarnessEvent, step_id: str, decision: Decision) -> Decision:
        from .ledger import Approval

        approval_id = _uid("appr")
        timeout = self.config.approval_timeout_sec
        timeout_at = time.time() + timeout
        fut = self.approvals.register(approval_id)
        await self.writer.submit(lambda: self.ledger.add_approval(
            Approval(approval_id=approval_id, step_id=step_id)))
        await self.writer.submit(lambda: self._set_step_status(step_id, "pending_approval", decision))
        self.bus.publish("approval", data={
            "approval_id": approval_id, "step_id": step_id, "tool_name": event.tool_name,
            "tool_input_preview": (decision.reason or "")[:200], "reason": decision.reason,
            "timeout_at": timeout_at,
        })
        resolution = await self.approvals.wait(approval_id, timeout)
        del fut  # the queue owns its lifecycle
        return await self._resolve_decision(approval_id, step_id, decision, resolution)

    async def _resolve_decision(
        self, approval_id: str, step_id: str, original: Decision, resolution: str
    ) -> Decision:
        # Always carry the ORIGINAL escalate reason through to the resolved decision, so the step
        # always shows *why* it escalated — even after it was approved (previously the reason was
        # overwritten with a bare "승인됨", losing the L2 cause).
        why = original.reason or "L2 검토 필요"
        cid = original.criterion_id  # keep pointing at the rule that originally fired
        layer = original.layer or 2
        if resolution == APPROVED:
            await self.writer.submit(lambda: self.ledger.resolve_approval(approval_id, "approved", "gui"))
            return Decision.allow(layer=layer, reason=f"승인됨 (escalate 해소) — 사유: {why}", criterion_id=cid)
        if resolution == DENIED:
            await self.writer.submit(lambda: self.ledger.resolve_approval(approval_id, "denied", "gui"))
            return Decision.deny(layer=layer, reason=f"거부됨 — 사유: {why}", criterion_id=cid)
        # timeout → apply default_on_timeout
        policy = self.config.default_on_timeout
        await self.writer.submit(lambda: self.ledger.resolve_approval(
            approval_id, "timeout", "policy", f"default_on_timeout={policy}"))
        if policy == TIMEOUT_ALLOW:
            return Decision.allow(layer=layer, reason=f"승인 타임아웃 — 기본 정책상 허용 · 원래 사유: {why}", criterion_id=cid)
        if policy == TIMEOUT_ESCALATE_TERMINAL:
            # Claude renders this as permissionDecision "ask"; Codex collapses to deny.
            return Decision.escalate(layer=layer, reason=f"승인 타임아웃 — 터미널로 에스컬레이션 · 사유: {why}", criterion_id=cid)
        return Decision.deny(layer=layer, reason=f"승인 타임아웃 — 기본 정책상 거부 · 원래 사유: {why}", criterion_id=cid)

    def resolve_approval(self, approval_id: str, resolution: str, reason: Optional[str] = None) -> bool:
        """External (REST/CLI) resolution entry point. Returns True if a waiter was unblocked."""
        return self.approvals.resolve(approval_id, resolution)

    # -- step decision persistence (writer coroutine) -------------------- #
    def _apply_decision(self, step_id: str, decision: Decision) -> None:
        step = self.ledger.get_step(step_id)
        if step is None:
            return
        step.decision = decision.action
        step.decision_layer = decision.layer
        step.decision_reason = decision.reason
        step.decision_criterion = decision.criterion_id
        if decision.action == DENY:
            step.status = "denied"
        elif step.status == "pending_approval":
            step.status = "running"
        if decision.downgrades:
            self.ledger.add_event(
                _uid("evt"), step.flow_id, "downgrade", time.time(),
                {"step_id": step_id, "downgrades": decision.downgrades},
            )
        self.ledger.upsert_step(step)
        self.bus.publish("upsert", "step", self._step_patch(step))

    def _set_step_status(self, step_id: str, status: str, decision: Decision) -> None:
        step = self.ledger.get_step(step_id)
        if step is None:
            return
        step.status = status
        step.decision = decision.action
        step.decision_layer = decision.layer
        step.decision_reason = decision.reason
        step.decision_criterion = decision.criterion_id
        self.ledger.upsert_step(step)
        self.bus.publish("upsert", "step", self._step_patch(step))

    # -- publish helpers (write to ledger + emit a bus patch) ------------ #
    def _publish_flow(self, flow: Flow) -> None:
        self.ledger.upsert_flow(flow)
        self.bus.publish("upsert", "flow", self.flow_payload(flow))

    def _publish_task(self, task: Task) -> None:
        self.ledger.upsert_task(task)
        self.bus.publish("upsert", "task", _row(task))

    def _publish_step(self, step: Step) -> None:
        self.ledger.upsert_step(step)
        self.bus.publish("upsert", "step", self._step_patch(step))

    def _step_patch(self, step: Step) -> dict:
        """Step row for a live patch, enriched with its service-harness attribution so the
        trajectory shows usage chips immediately (not only after a REST tree re-fetch)."""
        data = _row(step)
        data["harness_usage"] = self.usage_for_step(step.tool_name, step.tool_input, step.tool_output)
        return data

    # -- read models for REST -------------------------------------------- #
    def status_payload(self) -> dict:
        pending = self.ledger.pending_approvals()
        return {
            "mode": self.config.mode,
            "fail_open": self.config.fail_open,
            "pending_approvals": len(pending),
            "approval_timeout_sec": self.config.approval_timeout_sec,
            "default_on_timeout": self.config.default_on_timeout,
            "rev": self.bus.rev,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _json(value) -> Optional[str]:
    import json

    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _is_failure(event: HarnessEvent) -> bool:
    out = event.tool_output or {}
    if isinstance(out, dict):
        if out.get("error") or out.get("is_error") or out.get("success") is False:
            return True
    return bool(event.status and str(event.status).lower() in ("error", "failed", "failure"))


def _l3_status(tasks: list, l3: dict) -> dict:
    """Per-flow Layer-3 threshold status — which QA limits the run crossed. L3 never blocks a step
    (it drives alerting/AHE), so this surfaces *where* it was breached: failures vs failure_count_trigger,
    slow steps vs latency_multiplier×median, low Judge scores vs quality_threshold (when scored)."""
    steps: list = []

    def walk(ts):
        for t in ts:
            steps.extend(t.get("steps") or [])
            walk(t.get("children") or [])
    walk(tasks)

    failures = sum(1 for s in steps if s.get("status") == "failed")
    durs = sorted(s["duration_ms"] for s in steps if s.get("duration_ms"))
    median_d = durs[len(durs) // 2] if durs else None
    lat_mult = l3.get("latency_multiplier")
    slow = (sum(1 for s in steps if s.get("duration_ms") and s["duration_ms"] > lat_mult * median_d)
            if (median_d and lat_mult) else 0)
    quality = l3.get("quality_threshold")
    low_q = sum(1 for s in steps if s.get("judge_score") is not None and quality is not None
                and s["judge_score"] < quality)
    fail_trigger = l3.get("failure_count_trigger")
    out = {
        "failures": {"value": failures, "threshold": fail_trigger,
                     "breached": bool(fail_trigger and failures >= fail_trigger)},
        "slow": {"value": slow, "threshold": lat_mult, "breached": slow > 0},
        "low_quality": {"value": low_q, "threshold": quality,
                        "breached": bool(low_q)},
    }
    out["breached"] = any(v["breached"] for v in out.values() if isinstance(v, dict))
    return out


def _as_text(value) -> str:
    """Flatten a step's tool input/output (str or already-JSON) to text for usage attribution."""
    if value is None:
        return ""
    return value if isinstance(value, str) else (_json(value) or "")


def _resolve_component_path(kind: str, name: str, cwd: Optional[str]) -> Optional[Path]:
    """Locate the prompt file backing a (kind, name) service-harness component.

    Searches the project (cwd) first, then the user's home — under both ``.claude`` and ``.codex``
    where applicable — and returns the first existing file. Mirrors the vocabulary
    :func:`harness_lens.harness_usage.detect_usage` attributes from a step.
    """
    if not name:
        return None
    roots: list[Path] = []
    if cwd:
        roots.append(Path(cwd))
    roots.append(Path.home())

    def candidates(base: Path) -> list[Path]:
        claude, codex = base / ".claude", base / ".codex"
        if kind == "skill":
            return [claude / "skills" / name / "SKILL.md", codex / "skills" / name / "SKILL.md"]
        if kind == "command":
            return [claude / "commands" / f"{name}.md", claude / "prompts" / f"{name}.md",
                    codex / "commands" / f"{name}.md", codex / "prompts" / f"{name}.md"]
        if kind == "workflow":
            out: list[Path] = []
            for ext in (".md", ".markdown", ".js"):
                out += [claude / "workflows" / f"{name}{ext}", codex / "workflows" / f"{name}{ext}"]
            return out
        if kind == "plugin":
            return [claude / "plugins" / name / "SKILL.md", claude / "plugins" / name / "README.md"]
        if kind == "cursor_rule":
            return [base / ".cursor" / "rules" / f"{name}.mdc", base / ".cursor" / "rules" / f"{name}.md"]
        if kind in ("instruction", "import"):
            # name is e.g. "CLAUDE.md" / "AGENTS.md": project root, then ~/.claude, ~/.codex, ~.
            return [base / name, claude / name, codex / name]
        return []

    for base in roots:
        for cand in candidates(base):
            if cand.is_file():
                return cand
    return None


def _scan_cursor_rules(cwd: Optional[str]) -> list[dict]:
    """`.cursor/rules/**` are harness scaffolding even though Cursor is not a tracked runtime —
    include them in the audit scope (global + project), as service-harness components."""
    out: list[dict] = []
    bases = [(Path.home(), "전역")]
    if cwd:
        bases.append((Path(cwd), "프로젝트"))
    for base, scope in bases:
        rules = base / ".cursor" / "rules"
        if not rules.is_dir():
            continue
        names = sorted(p.name for p in rules.glob("**/*") if p.is_file())
        detail = f"{len(names)}개 규칙" + (": " + ", ".join(names[:5]) if names else "")
        out.append({"component": "cursor_rules", "kind": "커서 규칙", "scope": scope,
                    "path": str(rules), "exists": True, "editable": False, "detail": detail})
    return out


def _row(record) -> dict:
    from dataclasses import asdict, is_dataclass

    return asdict(record) if is_dataclass(record) else dict(record)
