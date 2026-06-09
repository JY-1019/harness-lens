"""Reconstruct Flow / Task / Step from a stream of hook events.

Claude Code has no notion of Flow/Task/Step; harness-lens reconstructs them
*after the fact* from the hook event order (design §2):

    SessionStart        → Flow start
    UserPromptSubmit    → new Task (prompt text = Task name)
      PreToolUse(Read/Glob/Grep)    → Task "수집" Step
      PreToolUse(Bash)              → Task "실행" Step
      PreToolUse(Write/Edit)        → Task "반영" Step
      PreToolUse(WebSearch/WebFetch)→ Task "조사" Step
      PreToolUse(mcp__*)            → Task "외부도구" Step
      PostToolUse(ok/fail)          → Step success / retry merge
    Stop                → Flow end candidate
    SessionEnd          → Flow confirmed end

Because each hook runs in its own process, the per-session cursor (current flow /
task / category / open step) is persisted via ``store.{get,set}_cursor`` rather
than kept in memory.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

from .store import Prompt, PromptSnapshot, Session, Step, StorageBackend, new_id

_SUMMARY_LIMIT = 500
# Requests are stored more generously than step summaries: the GUI shows "what was asked",
# so keep enough of the prompt to be meaningful while still bounding the row size.
_PROMPT_LIMIT = 4000


def _truncate(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Reconstructor:
    """Stateful (store-backed) reconstruction of hook events.

    A ``criteria_engine`` collaborator, when supplied, evaluates each completed
    step (Layer 1 always, Layer 2 sampled). It is optional so the reconstructor
    stays usable in dependency-light contexts (e.g. unit tests).
    """

    TOOL_CATEGORY = {
        "Read": "수집", "Glob": "수집", "Grep": "수집",
        "Bash": "실행",
        "Write": "반영", "Edit": "반영", "MultiEdit": "반영", "NotebookEdit": "반영",
        "WebSearch": "조사", "WebFetch": "조사",
    }
    DEFAULT_PLATFORM = "claude-code"
    DEFAULT_CATEGORY = "기타"
    # The global agent-instruction file feeding the system prompt; snapshotted per Flow.
    GLOBAL_INSTRUCTION = Path.home() / ".claude" / "CLAUDE.md"

    def __init__(self, store: StorageBackend, criteria_engine=None):
        self.store = store
        self.criteria_engine = criteria_engine

    def categorize(self, tool_name: str) -> str:
        if tool_name.startswith("mcp__"):
            return "외부도구"
        return self.TOOL_CATEGORY.get(tool_name, self.DEFAULT_CATEGORY)

    def _observed(self, tool_name: str) -> bool:
        """Whether a step for ``tool_name`` is fully observed (vs a gap).

        Claude Code's PreToolUse intercepts every tool, so every step is observed. The
        Codex subclass overrides this to flag tools its hooks cannot surface.
        """
        return True

    # -- Flow lifecycle -------------------------------------------------- #
    def on_session_start(self, session_id: str, platform: Optional[str] = None) -> Session:
        session = self.store.get_session(session_id)
        if session is None:
            session = Session(
                session_id=session_id,
                platform=platform or self.DEFAULT_PLATFORM,
                started_at=time.time(),
                status="active",
            )
            self.store.upsert_session(session)
            self._snapshot_system_prompt(session_id)
        self.store.set_cursor(
            session_id,
            flow_id=new_id("flow"),
            current_task_id=None, current_task_name=None, current_category=None,
            current_step_id=None, pending_task=0, last_stop_at=None,
        )
        return session

    def _snapshot_system_prompt(self, session_id: str) -> None:
        """Snapshot the instruction files in effect for this Flow (best-effort).

        The system prompt an agent runs under is the union of the *global* instruction file and
        any *project-local* one (cwd) — so both are snapshotted (distinct scopes). Records
        content + sha so the monitor can show which instructions applied and diff them across
        Flows. Never raises: a missing/unreadable file just records nothing.
        """
        self._snapshot_instruction(session_id, "전역", self.GLOBAL_INSTRUCTION)
        # Project-local CLAUDE.md/AGENTS.md (cwd) also feeds the prompt; snapshot it when it is a
        # different file from the global one so a project instruction is tracked, not just global.
        project = Path.cwd() / self.GLOBAL_INSTRUCTION.name
        try:
            distinct = project.resolve() != self.GLOBAL_INSTRUCTION.resolve()
        except OSError:
            distinct = True
        if distinct:
            self._snapshot_instruction(session_id, "프로젝트", project)

    def _snapshot_instruction(self, session_id: str, scope: str, path: Path) -> None:
        from .enforce import MARKER_START

        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return
        snippet = content if len(content) <= 20000 else content[:20000] + "\n…(truncated)"
        self.store.add_prompt_snapshot(PromptSnapshot(
            session_id=session_id,
            scope=scope,
            path=str(path),
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest()[:12],
            line_count=len(content.splitlines()),
            managed_block=MARKER_START in content,
            content=snippet,
        ))

    def on_user_prompt(self, session_id: str, prompt: str) -> None:
        self._ensure_session(session_id)
        self.store.set_cursor(
            session_id,
            current_task_name=_truncate(prompt, 160),
            pending_task=1,
            last_stop_at=None,  # a new prompt cancels a pending Stop
        )
        self._record_prompt(session_id, prompt)

    def _record_prompt(self, session_id: str, prompt: str) -> None:
        """Persist the just-submitted request durably (the cursor only holds it transiently).

        The cursor's ``current_task_name`` is overwritten by the next prompt, so the originating
        request of each Task would otherwise be unrecoverable. Tag it with the Flow it opened;
        ``on_pre_tool`` later links it to the Task its first tool call creates. Read the flow_id
        *after* the cursor is set so a Codex prompt that opened a fresh Flow is attributed to it.
        Best-effort — a recording failure must never break observation.
        """
        text = _truncate(prompt, _PROMPT_LIMIT)
        if not text:
            return
        cursor = self.store.get_cursor(session_id)
        try:
            self.store.add_prompt(Prompt(
                session_id=session_id,
                flow_id=cursor.get("flow_id") or "",
                text=text,
            ))
        except Exception:
            pass

    def on_pre_tool(self, session_id: str, tool_name: str, input_summary: str = "") -> Step:
        self._ensure_session(session_id)
        cursor = self.store.get_cursor(session_id)
        category = self.categorize(tool_name)
        pending = bool(cursor.get("pending_task"))

        # Retry merge: same tool failed and was the most recent step *within the same
        # task*. A new prompt (pending_task) starts a fresh task, so never merge across it.
        open_id = cursor.get("current_step_id")
        if not pending and open_id:
            last = self.store.get_step(open_id)
            # A retry repeats the *same call*: same tool AND same input. Matching on tool
            # name alone would fold an unrelated next call (Bash(pytest) fail → Bash(ls) ok)
            # into the failed step and erase the real failure.
            same_call = last is not None and last.tool_name == tool_name \
                and last.input_summary == _truncate(input_summary)
            if same_call and last.success is False:
                last.retry_count += 1
                last.success = None
                # The reused row is re-evaluated as the retry resolves; clearing prior
                # Layer-1/2 results prevents a stale low score from the failed attempt
                # sticking to a step that ultimately succeeds (and is not re-sampled).
                last.layer1_passed = None
                last.layer2_score = None
                last.input_summary = _truncate(input_summary) or last.input_summary
                # The retry overwrites this row's output, so any Judge sample taken on the
                # previous failed attempt is now stale. Drop it — otherwise `review` could
                # ask a human to label a score produced from output that no longer exists,
                # skewing Judge drift.
                self.store.delete_judge_samples_for_step(last.step_id)
                self.store.update_step(last)
                return last

        new_task = pending or category != cursor.get("current_category")
        task_id = new_id("task") if new_task or not cursor.get("current_task_id") else cursor["current_task_id"]

        step = Step(
            session_id=session_id,
            flow_id=cursor.get("flow_id") or new_id("flow"),
            task_id=task_id,
            task_category=category,
            tool_name=tool_name,
            input_summary=_truncate(input_summary),
            success=None,
            observed=self._observed(tool_name),
            timestamp=time.time(),
        )
        self.store.add_step(step)
        self.store.set_cursor(
            session_id,
            current_task_id=task_id,
            current_category=category,
            current_step_id=step.step_id,
            pending_task=0,
        )
        # A pending prompt means this tool call is the first step of the Task that prompt opened;
        # bind the request to the Task so the GUI can show each Task's originating request.
        if pending:
            self.store.link_latest_prompt_to_task(session_id, task_id)
        return step

    def on_post_tool(
        self,
        session_id: str,
        tool_name: str,
        output_summary: str = "",
        success: bool = True,
        latency_ms: Optional[int] = None,
        input_summary: str = "",
    ) -> Optional[Step]:
        cursor = self.store.get_cursor(session_id)
        open_id = cursor.get("current_step_id")
        step = self.store.get_step(open_id) if open_id else None

        # Defensive: a PostToolUse without a matching PreToolUse still records a step.
        # The cursor step must still be *open* (success is None) to be reused; a closed
        # step of the same tool belongs to a prior call and must not be overwritten when
        # a pre-hook is missed (e.g. after a transient pre-hook timeout). The post event
        # carries tool_input too, so forward it — otherwise the synthetic step records an
        # empty input_summary and the Debugger loses the command/file-path evidence.
        if step is None or step.tool_name != tool_name or step.success is not None:
            step = self.on_pre_tool(session_id, tool_name, input_summary)

        step.output_summary = _truncate(output_summary)
        step.success = bool(success)
        step.latency_ms = latency_ms
        self.store.update_step(step)

        if self.criteria_engine is not None:
            context = self.store.steps_for_session(session_id)[:-1]
            self.criteria_engine.evaluate(step, context)
            self.store.update_step(step)
        return step

    def on_stop(self, session_id: str) -> None:
        """Mark a Flow-end candidate and finalize the session's derived status.

        SessionEnd is the authoritative close (it also records tokens), but some Claude
        Code builds/scenarios never emit it. If only Stop fires, deriving status here keeps
        the session from being stuck ``active`` forever — otherwise ``show --fail`` and the
        status views never reflect a completed/failed run. SessionEnd, when it arrives,
        refines this. Stop fires per turn, so ``ended_at`` tracks the latest turn boundary.
        """
        self.store.set_cursor(session_id, last_stop_at=time.time())
        session = self.store.get_session(session_id)
        if session is None:
            return
        session.ended_at = time.time()
        session.status = self._derive_status(session_id)
        self.store.upsert_session(session)

    def on_session_end(
        self,
        session_id: str,
        total_tokens: int = 0,
        status: Optional[str] = None,
    ) -> Optional[Session]:
        session = self.store.get_session(session_id)
        if session is None:
            return None
        session.ended_at = time.time()
        session.total_tokens = total_tokens or session.total_tokens
        session.status = status or self._derive_status(session_id)
        self.store.upsert_session(session)
        return session

    # -- helpers --------------------------------------------------------- #
    def _ensure_session(self, session_id: str) -> Session:
        session = self.store.get_session(session_id)
        if session is None:
            session = self.on_session_start(session_id)
        return session

    def _derive_status(self, session_id: str) -> str:
        steps = self.store.steps_for_session(session_id)
        return "failed" if any(s.success is False for s in steps) else "completed"


class CodexReconstructor(Reconstructor):
    """Codex CLI reconstruction (design Codex §1, §2, §9).

    Codex differs from Claude Code in ways that change reconstruction:

    * **No SessionEnd.** ``Stop`` is the only terminator, but it may be a pause rather than
      a real end, so a ``UserPromptSubmit`` within :data:`STOP_REOPEN_WINDOW_SEC` resumes the
      same Flow; a later prompt begins a fresh Flow in the same session.
    * **PreToolUse only intercepts Bash / apply_patch / MCP.** Other tools (e.g. web search)
      surface, if at all, only at PostToolUse. A step for an unrecognized tool is categorized
      ``관측불가`` and flagged ``observed=False`` — the gap is recorded, never guessed.
    * **Edits use ``apply_patch``** instead of Write/Edit.
    """

    TOOL_CATEGORY = {
        "Bash": "실행",
        "apply_patch": "반영",
    }
    DEFAULT_PLATFORM = "codex"
    DEFAULT_CATEGORY = "관측불가"
    GLOBAL_INSTRUCTION = Path.home() / ".codex" / "AGENTS.md"
    STOP_REOPEN_WINDOW_SEC = 30

    def _observed(self, tool_name: str) -> bool:
        # Bash / apply_patch / mcp__* are reliably intercepted; anything falling through to
        # the gap category was not properly observed.
        return self.categorize(tool_name) != self.DEFAULT_CATEGORY

    def on_user_prompt(self, session_id: str, prompt: str) -> None:
        session = self._ensure_session(session_id)
        cursor = self.store.get_cursor(session_id)
        last_stop = cursor.get("last_stop_at")
        fields = dict(
            current_task_name=_truncate(prompt, 160),
            pending_task=1,
            last_stop_at=None,  # a new prompt cancels a pending Stop
        )
        beyond_window = last_stop is not None and (time.time() - last_stop) > self.STOP_REOPEN_WINDOW_SEC
        if beyond_window:
            # The prior Stop was a real Flow end. With no SessionEnd to delimit Flows, open a
            # fresh Flow within the same session rather than reusing the closed one.
            fields.update(
                flow_id=new_id("flow"),
                current_task_id=None,
                current_category=None,
                current_step_id=None,
            )
        # Resuming after a Stop: reactivate the session that on_stop tentatively finalized.
        if session.status != "active":
            session.status = "active"
            session.ended_at = None
            self.store.upsert_session(session)
        self.store.set_cursor(session_id, **fields)
        # Record after the cursor is set so a prompt that opened a fresh Flow (beyond the
        # reopen window) is attributed to that new flow_id, not the closed one.
        self._record_prompt(session_id, prompt)
