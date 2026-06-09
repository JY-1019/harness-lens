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
from .policy import PolicyContext, PolicyEngine, read_path_of
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
        return self.scopes_payload()

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
            read_paths=set(st.read_paths), failed_steps=st.failed_steps, total_steps=st.total_steps
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
        if resolution == APPROVED:
            await self.writer.submit(lambda: self.ledger.resolve_approval(approval_id, "approved", "gui"))
            return Decision.allow(layer=2, reason="승인됨 (escalate 해소)")
        if resolution == DENIED:
            await self.writer.submit(lambda: self.ledger.resolve_approval(approval_id, "denied", "gui"))
            return Decision.deny(layer=2, reason=original.reason or "거부됨")
        # timeout → apply default_on_timeout
        policy = self.config.default_on_timeout
        await self.writer.submit(lambda: self.ledger.resolve_approval(
            approval_id, "timeout", "policy", f"default_on_timeout={policy}"))
        if policy == TIMEOUT_ALLOW:
            return Decision.allow(layer=2, reason="승인 타임아웃 — 기본 정책상 허용")
        if policy == TIMEOUT_ESCALATE_TERMINAL:
            # Claude renders this as permissionDecision "ask"; Codex downgrades to deny.
            return Decision.escalate(layer=2, reason="승인 타임아웃 — 터미널로 에스컬레이션")
        return Decision.deny(layer=2, reason="승인 타임아웃 — 기본 정책상 거부")

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
        self.bus.publish("upsert", "step", _row(step))

    def _set_step_status(self, step_id: str, status: str, decision: Decision) -> None:
        step = self.ledger.get_step(step_id)
        if step is None:
            return
        step.status = status
        step.decision = decision.action
        step.decision_layer = decision.layer
        step.decision_reason = decision.reason
        self.ledger.upsert_step(step)
        self.bus.publish("upsert", "step", _row(step))

    # -- publish helpers (write to ledger + emit a bus patch) ------------ #
    def _publish_flow(self, flow: Flow) -> None:
        self.ledger.upsert_flow(flow)
        self.bus.publish("upsert", "flow", _row(flow))

    def _publish_task(self, task: Task) -> None:
        self.ledger.upsert_task(task)
        self.bus.publish("upsert", "task", _row(task))

    def _publish_step(self, step: Step) -> None:
        self.ledger.upsert_step(step)
        self.bus.publish("upsert", "step", _row(step))

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


def _row(record) -> dict:
    from dataclasses import asdict, is_dataclass

    return asdict(record) if is_dataclass(record) else dict(record)
