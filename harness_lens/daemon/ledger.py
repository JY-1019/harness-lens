"""The Flow/Task/Step ledger — the daemon's durable record.

This is the schema the GUI spec defines as the source of truth: ``flows`` (= session),
``tasks`` (turn or subagent, self-referencing for nested subagents), ``steps`` (a tool
call), ``approvals`` (escalate resolutions), and ``events`` (raw HarnessEvent for audit
/ replay). It lives in the same ``ledger.db`` as the legacy observe-only tables; the two
coexist and :func:`migrate_legacy` back-fills the new tree tables from the old
``sessions``/``steps`` so historical Flows remain viewable.

Schema is versioned in ``schema_meta`` and applied as an ordered list of migrations, so a
later build can add a step without rewriting the table.

Concurrency: SQLite in WAL mode allows many readers with one writer. All access here is
guarded by a process-wide lock and the connection is opened ``check_same_thread=False`` so
the FastAPI read handlers and the single ledger-writer task can share one instance safely.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .. import home_dir
from .events import mask_secrets

# Step/output sizes: a tool_output bigger than this is truncated in the ledger (the full body
# stays in the transcript, which the GUI lazy-loads — design: don't bloat the tree patch).
_OUTPUT_LIMIT = 8 * 1024


def default_db_path() -> Path:
    return home_dir() / "ledger.db"


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Flow:
    flow_id: str
    source: str
    status: str = "running"  # running|completed|failed|aborted
    mode: str = "observe"
    started_at: float = field(default_factory=time.time)
    title: Optional[str] = None
    cwd: Optional[str] = None
    model: Optional[str] = None
    ended_at: Optional[float] = None
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    l2_score: Optional[float] = None


@dataclass
class Task:
    task_id: str
    flow_id: str
    kind: str  # 'turn' | 'subagent'
    status: str = "running"  # running|completed|failed|blocked
    seq: int = 0
    parent_task_id: Optional[str] = None
    agent_name: Optional[str] = None
    title: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    retry_count: int = 0
    l2_score: Optional[float] = None


@dataclass
class Step:
    step_id: str
    task_id: str
    flow_id: str
    tool_name: str
    status: str = "running"  # pending_approval|running|ok|failed|denied
    seq: int = 0
    tool_input: Optional[str] = None
    tool_output: Optional[str] = None
    decision: Optional[str] = None  # allow|deny|escalate
    decision_layer: Optional[int] = None
    decision_reason: Optional[str] = None
    decision_criterion: Optional[str] = None  # which rule fired: L2 criterion id / L1 invariant / L3 key
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    duration_ms: Optional[int] = None
    tokens: Optional[int] = None
    judge_score: Optional[float] = None
    judge_reason: Optional[str] = None


@dataclass
class Approval:
    approval_id: str
    step_id: str
    requested_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    resolution: Optional[str] = None  # approved|denied|timeout|daemon_restart
    resolved_by: Optional[str] = None  # gui|terminal|policy
    reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Schema + migrations
# --------------------------------------------------------------------------- #
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS flows (
  flow_id        TEXT PRIMARY KEY,
  source         TEXT NOT NULL,
  title          TEXT,
  cwd            TEXT,
  model          TEXT,
  status         TEXT NOT NULL,
  mode           TEXT NOT NULL,
  started_at     REAL NOT NULL,
  ended_at       REAL,
  total_tokens   INTEGER DEFAULT 0,
  total_cost_usd REAL DEFAULT 0,
  l2_score       REAL
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id        TEXT PRIMARY KEY,
  flow_id        TEXT NOT NULL REFERENCES flows,
  parent_task_id TEXT REFERENCES tasks,
  kind           TEXT NOT NULL,
  agent_name     TEXT,
  title          TEXT,
  status         TEXT NOT NULL,
  started_at     REAL NOT NULL,
  ended_at       REAL,
  retry_count    INTEGER DEFAULT 0,
  l2_score       REAL,
  seq            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
  step_id        TEXT PRIMARY KEY,
  task_id        TEXT NOT NULL REFERENCES tasks,
  flow_id        TEXT NOT NULL REFERENCES flows,
  tool_name      TEXT NOT NULL,
  tool_input     TEXT,
  tool_output    TEXT,
  status         TEXT NOT NULL,
  decision       TEXT,
  decision_layer INTEGER,
  decision_reason TEXT,
  started_at     REAL NOT NULL,
  ended_at       REAL,
  duration_ms    INTEGER,
  tokens         INTEGER,
  judge_score    REAL,
  judge_reason   TEXT,
  seq            INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id    TEXT PRIMARY KEY,
  step_id        TEXT NOT NULL REFERENCES steps,
  requested_at   REAL NOT NULL,
  resolved_at    REAL,
  resolution     TEXT,
  resolved_by    TEXT,
  reason         TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  flow_id  TEXT,
  kind     TEXT,
  ts       REAL,
  raw      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_flow ON tasks(flow_id, seq);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_flows_started ON flows(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_step ON approvals(step_id);
"""

# Track which specific rule decided a step, so the GUI can pinpoint the fired L2 criterion / L1
# invariant among many. NULL for pre-existing rows (decision_reason still carries the text).
_SCHEMA_V2 = "ALTER TABLE steps ADD COLUMN decision_criterion TEXT;"

# (version, sql). Append-only: never edit a shipped migration; add the next one.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
)
SCHEMA_VERSION = _MIGRATIONS[-1][0]


class DaemonLedger:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=OFF")  # tree refs are app-managed; avoid insert-order traps
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "DaemonLedger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- schema ---------------------------------------------------------- #
    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
            row = self._conn.execute("SELECT MAX(version) AS v FROM schema_meta").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            for version, sql in _MIGRATIONS:
                if version > current:
                    self._conn.executescript(sql)
                    self._conn.execute("INSERT INTO schema_meta(version) VALUES(?)", (version,))
            self._conn.commit()

    # -- flows ----------------------------------------------------------- #
    def upsert_flow(self, flow: Flow) -> Flow:
        with self._lock:
            self._conn.execute(
                """INSERT INTO flows(flow_id, source, title, cwd, model, status, mode,
                       started_at, ended_at, total_tokens, total_cost_usd, l2_score)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(flow_id) DO UPDATE SET
                     source=excluded.source, title=COALESCE(excluded.title, flows.title),
                     cwd=COALESCE(excluded.cwd, flows.cwd), model=COALESCE(excluded.model, flows.model),
                     status=excluded.status, mode=excluded.mode, ended_at=excluded.ended_at,
                     total_tokens=excluded.total_tokens, total_cost_usd=excluded.total_cost_usd,
                     l2_score=COALESCE(excluded.l2_score, flows.l2_score)""",
                (flow.flow_id, flow.source, flow.title, flow.cwd, flow.model, flow.status,
                 flow.mode, flow.started_at, flow.ended_at, flow.total_tokens,
                 flow.total_cost_usd, flow.l2_score),
            )
            self._conn.commit()
        return flow

    def get_flow(self, flow_id: str) -> Optional[Flow]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM flows WHERE flow_id=?", (flow_id,)).fetchone()
        return _flow(row) if row else None

    def list_flows(self, limit: int = 50, status: Optional[str] = None,
                   source: Optional[str] = None, has_cwd: bool = False) -> list[Flow]:
        sql = "SELECT * FROM flows"
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if source:
            clauses.append("source=?")
            params.append(source)
        if has_cwd:
            # Only sessions tied to a real project folder — drops cwd-less subagent/tool sessions.
            clauses.append("cwd IS NOT NULL AND cwd != ''")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_flow(r) for r in rows]

    def latest_running_turn(self, flow_id: str) -> Optional[str]:
        """The most recent still-open turn task of a Flow — used to rehydrate in-memory state
        after a daemon restart so an in-progress session resumes its turn (keeping its title)
        instead of spawning a titleless "(요청 미관측)" turn for the next tool."""
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id FROM tasks WHERE flow_id=? AND kind='turn' AND status='running' "
                "ORDER BY seq DESC LIMIT 1",
                (flow_id,),
            ).fetchone()
        return row["task_id"] if row else None

    # -- tasks ----------------------------------------------------------- #
    def upsert_task(self, task: Task) -> Task:
        with self._lock:
            self._conn.execute(
                """INSERT INTO tasks(task_id, flow_id, parent_task_id, kind, agent_name, title,
                       status, started_at, ended_at, retry_count, l2_score, seq)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     parent_task_id=COALESCE(excluded.parent_task_id, tasks.parent_task_id),
                     kind=excluded.kind, agent_name=COALESCE(excluded.agent_name, tasks.agent_name),
                     title=COALESCE(excluded.title, tasks.title), status=excluded.status,
                     ended_at=excluded.ended_at, retry_count=excluded.retry_count,
                     l2_score=COALESCE(excluded.l2_score, tasks.l2_score)""",
                (task.task_id, task.flow_id, task.parent_task_id, task.kind, task.agent_name,
                 task.title, task.status, task.started_at, task.ended_at, task.retry_count,
                 task.l2_score, task.seq),
            )
            self._conn.commit()
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return _task(row) if row else None

    def next_task_seq(self, flow_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS m FROM tasks WHERE flow_id=?", (flow_id,)
            ).fetchone()
        return int(row["m"]) + 1

    # -- steps ----------------------------------------------------------- #
    def upsert_step(self, step: Step) -> Step:
        # Truncate an oversized output before it lands; the full body stays in the transcript.
        if step.tool_output is not None and len(step.tool_output) > _OUTPUT_LIMIT:
            step.tool_output = step.tool_output[:_OUTPUT_LIMIT] + "\n…(truncated; see transcript)"
        with self._lock:
            self._conn.execute(
                """INSERT INTO steps(step_id, task_id, flow_id, tool_name, tool_input, tool_output,
                       status, decision, decision_layer, decision_reason, decision_criterion,
                       started_at, ended_at, duration_ms, tokens, judge_score, judge_reason, seq)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(step_id) DO UPDATE SET
                     tool_name=excluded.tool_name,
                     tool_input=COALESCE(excluded.tool_input, steps.tool_input),
                     tool_output=COALESCE(excluded.tool_output, steps.tool_output),
                     status=excluded.status,
                     decision=COALESCE(excluded.decision, steps.decision),
                     decision_layer=COALESCE(excluded.decision_layer, steps.decision_layer),
                     decision_reason=COALESCE(excluded.decision_reason, steps.decision_reason),
                     decision_criterion=COALESCE(excluded.decision_criterion, steps.decision_criterion),
                     ended_at=excluded.ended_at, duration_ms=excluded.duration_ms,
                     tokens=COALESCE(excluded.tokens, steps.tokens),
                     judge_score=COALESCE(excluded.judge_score, steps.judge_score),
                     judge_reason=COALESCE(excluded.judge_reason, steps.judge_reason)""",
                (step.step_id, step.task_id, step.flow_id, step.tool_name, step.tool_input,
                 step.tool_output, step.status, step.decision, step.decision_layer,
                 step.decision_reason, step.decision_criterion, step.started_at, step.ended_at,
                 step.duration_ms, step.tokens, step.judge_score, step.judge_reason, step.seq),
            )
            self._conn.commit()
            self._recompute_flow_totals(step.flow_id)
        return step

    def get_step(self, step_id: str) -> Optional[Step]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM steps WHERE step_id=?", (step_id,)).fetchone()
        return _step(row) if row else None

    def next_step_seq(self, task_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) AS m FROM steps WHERE task_id=?", (task_id,)
            ).fetchone()
        return int(row["m"]) + 1

    def _recompute_flow_totals(self, flow_id: str) -> None:
        # flow.total_tokens accrues from its steps (design: writer-side accumulation). Only
        # overwrite when steps actually carry token counts — otherwise a flow total supplied by
        # the JSONL tail or session_end (the usual source, since hooks rarely carry per-step
        # tokens) would be zeroed on every step upsert.
        row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) AS t FROM steps WHERE flow_id=? AND tokens IS NOT NULL",
            (flow_id,),
        ).fetchone()
        if row["t"]:
            self._conn.execute(
                "UPDATE flows SET total_tokens=? WHERE flow_id=?", (int(row["t"]), flow_id)
            )
            self._conn.commit()

    def set_flow_tokens(self, flow_id: str, total_tokens: int) -> None:
        """Set a flow's token total directly (JSONL-tail enrichment path)."""
        with self._lock:
            self._conn.execute(
                "UPDATE flows SET total_tokens=? WHERE flow_id=?", (int(total_tokens), flow_id)
            )
            self._conn.commit()

    # -- events ---------------------------------------------------------- #
    def add_event(self, event_id: str, flow_id: Optional[str], kind: str, ts: float, raw: dict) -> None:
        # Mask secrets in the preserved raw payload before it is written (design constraint).
        safe = mask_secrets(raw)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO events(event_id, flow_id, kind, ts, raw) VALUES(?,?,?,?,?)",
                (event_id, flow_id, kind, ts, json.dumps(safe, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def events_since(self, after_ts: float = 0.0, limit: int = 200) -> list[dict]:
        """Recent raw events newer than ``after_ts`` (for ``harness-lens tail``)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, flow_id, kind, ts, raw FROM events WHERE ts > ? "
                "ORDER BY ts ASC LIMIT ?", (after_ts, limit),
            ).fetchall()
        out = []
        for r in rows:
            try:
                raw = json.loads(r["raw"]) if r["raw"] else {}
            except json.JSONDecodeError:
                raw = {}
            out.append({"event_id": r["event_id"], "flow_id": r["flow_id"],
                        "kind": r["kind"], "ts": r["ts"], "raw": raw})
        return out

    # -- approvals ------------------------------------------------------- #
    def add_approval(self, approval: Approval) -> Approval:
        with self._lock:
            self._conn.execute(
                """INSERT INTO approvals(approval_id, step_id, requested_at, resolved_at,
                       resolution, resolved_by, reason) VALUES(?,?,?,?,?,?,?)""",
                (approval.approval_id, approval.step_id, approval.requested_at,
                 approval.resolved_at, approval.resolution, approval.resolved_by, approval.reason),
            )
            self._conn.commit()
        return approval

    def resolve_approval(self, approval_id: str, resolution: str, resolved_by: str,
                         reason: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE approvals SET resolved_at=?, resolution=?, resolved_by=?, reason=?
                   WHERE approval_id=? AND resolved_at IS NULL""",
                (time.time(), resolution, resolved_by, reason, approval_id),
            )
            self._conn.commit()

    def pending_approvals(self) -> list[Approval]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE resolved_at IS NULL ORDER BY requested_at ASC"
            ).fetchall()
        return [_approval(r) for r in rows]

    def deny_all_pending(self, reason: str = "daemon restart") -> int:
        """On daemon start, any approval still pending from a previous run is denied.

        A blocked hook from the prior run is gone (its process died), so leaving the row
        pending would be misleading; the design mandates denying them with a recorded reason.
        """
        with self._lock:
            cur = self._conn.execute(
                """UPDATE approvals SET resolved_at=?, resolution='daemon_restart',
                       resolved_by='policy', reason=? WHERE resolved_at IS NULL""",
                (time.time(), reason),
            )
            # Mark the orphaned steps denied too, so the tree does not show an eternal pending node.
            self._conn.execute(
                """UPDATE steps SET status='denied',
                       decision='deny', decision_reason=COALESCE(decision_reason, ?)
                   WHERE status='pending_approval'""",
                (reason,),
            )
            self._conn.commit()
            return cur.rowcount

    # -- tree read model ------------------------------------------------- #
    def flow_tree(self, flow_id: str) -> Optional[dict]:
        flow = self.get_flow(flow_id)
        if flow is None:
            return None
        with self._lock:
            task_rows = self._conn.execute(
                "SELECT * FROM tasks WHERE flow_id=? ORDER BY seq ASC", (flow_id,)
            ).fetchall()
            step_rows = self._conn.execute(
                "SELECT * FROM steps WHERE flow_id=? ORDER BY seq ASC", (flow_id,)
            ).fetchall()
        steps_by_task: dict[str, list[dict]] = {}
        for r in step_rows:
            # Tree payload omits the (potentially large) output body — the GUI lazy-loads it
            # from GET /api/steps/{id}. Keep a short preview only.
            sd = asdict(_step(r))
            sd["tool_output"] = _preview(sd.get("tool_output"))
            steps_by_task.setdefault(r["task_id"], []).append(sd)
        nodes = {t["task_id"]: {**asdict(_task(t)), "steps": steps_by_task.get(t["task_id"], []),
                                "children": []} for t in task_rows}
        roots = []
        for t in task_rows:
            node = nodes[t["task_id"]]
            parent = t["parent_task_id"]
            if parent and parent in nodes:
                nodes[parent]["children"].append(node)
            else:
                roots.append(node)
        return {**asdict(flow), "tasks": roots}


# --------------------------------------------------------------------------- #
# Row → record
# --------------------------------------------------------------------------- #
def _preview(text: Optional[str], limit: int = 200) -> Optional[str]:
    if not text:
        return text
    return text if len(text) <= limit else text[:limit] + "…"


def _flow(row: sqlite3.Row) -> Flow:
    return Flow(
        flow_id=row["flow_id"], source=row["source"], title=row["title"], cwd=row["cwd"],
        model=row["model"], status=row["status"], mode=row["mode"], started_at=row["started_at"],
        ended_at=row["ended_at"], total_tokens=row["total_tokens"] or 0,
        total_cost_usd=row["total_cost_usd"] or 0.0, l2_score=row["l2_score"],
    )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"], flow_id=row["flow_id"], parent_task_id=row["parent_task_id"],
        kind=row["kind"], agent_name=row["agent_name"], title=row["title"], status=row["status"],
        started_at=row["started_at"], ended_at=row["ended_at"], retry_count=row["retry_count"] or 0,
        l2_score=row["l2_score"], seq=row["seq"],
    )


def _step(row: sqlite3.Row) -> Step:
    return Step(
        step_id=row["step_id"], task_id=row["task_id"], flow_id=row["flow_id"],
        tool_name=row["tool_name"], tool_input=row["tool_input"], tool_output=row["tool_output"],
        status=row["status"], decision=row["decision"], decision_layer=row["decision_layer"],
        decision_reason=row["decision_reason"], decision_criterion=row["decision_criterion"],
        started_at=row["started_at"], ended_at=row["ended_at"],
        duration_ms=row["duration_ms"], tokens=row["tokens"], judge_score=row["judge_score"],
        judge_reason=row["judge_reason"], seq=row["seq"],
    )


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        approval_id=row["approval_id"], step_id=row["step_id"], requested_at=row["requested_at"],
        resolved_at=row["resolved_at"], resolution=row["resolution"],
        resolved_by=row["resolved_by"], reason=row["reason"],
    )


# --------------------------------------------------------------------------- #
# Legacy migration
# --------------------------------------------------------------------------- #
# The legacy observe-only store and the new tree schema both want a table named ``steps``
# with incompatible columns, so they cannot share one file. The new schema owns ``ledger.db``;
# the legacy DB is relocated here and imported. Legacy reads (``show``/``status``/``gui`` via
# LensService) transparently fall back to this file — see store.default_db_path.
def legacy_db_path(root: Optional[Path] = None) -> Path:
    return (root or home_dir()) / "ledger.legacy.db"


_PLATFORM_TO_SOURCE = {"claude-code": "claude_code", "codex": "codex"}
_LEGACY_STATUS_TO_FLOW = {"active": "running", "completed": "completed", "failed": "failed"}


def _has_legacy_schema(conn: sqlite3.Connection) -> bool:
    """True if ``conn`` is an old observe-only ledger (a ``steps`` table with legacy columns)."""
    if not _table_exists(conn, "steps") or not _table_exists(conn, "sessions"):
        return False
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
    return "session_id" in cols and "task_category" in cols


def relocate_legacy_db(root: Optional[Path] = None) -> Optional[Path]:
    """If ``ledger.db`` is a legacy observe-only ledger, move it aside to ``ledger.legacy.db``.

    Returns the relocated path (so :func:`migrate_legacy` can import from it), or None when
    there is nothing to relocate (fresh install, or already a new-schema ledger.db). Safe to
    call repeatedly: once relocated, ``ledger.db`` no longer has the legacy schema.
    """
    root = root or home_dir()
    live = root / "ledger.db"
    target = legacy_db_path(root)
    if not live.exists() or target.exists():
        return target if target.exists() else None
    probe = sqlite3.connect(str(live))
    probe.row_factory = sqlite3.Row
    try:
        is_legacy = _has_legacy_schema(probe)
    finally:
        probe.close()
    if not is_legacy:
        return None
    live.rename(target)
    # WAL/shm sidecars belong to the old file; move them too so the relocated DB is consistent.
    for suffix in ("-wal", "-shm"):
        side = live.with_name(live.name + suffix)
        if side.exists():
            side.rename(target.with_name(target.name + suffix))
    return target


def migrate_legacy(ledger: DaemonLedger, source_path: Optional[Path] = None) -> dict:
    """Back-fill the new tree tables from a relocated legacy ledger (``ledger.legacy.db``).

    Idempotent: a session already present as a flow is skipped, so re-running after new legacy
    sessions accrue only imports the delta. Fields the old schema never had (tool_use_id, real
    durations, subagent structure) are left NULL — every legacy step becomes a ``turn`` task's
    step. Returns a small summary for the CLI/report.
    """
    source_path = source_path or legacy_db_path(ledger.db_path.parent)
    summary = {"flows": 0, "tasks": 0, "steps": 0, "skipped": 0}
    if not Path(source_path).exists():
        return summary
    src = sqlite3.connect(str(source_path))
    src.row_factory = sqlite3.Row
    try:
        if not _has_legacy_schema(src):
            return summary
        sessions = src.execute("SELECT * FROM sessions").fetchall()
        for srow in sessions:
            sid = srow["session_id"]
            if ledger.get_flow(sid) is not None:
                summary["skipped"] += 1
                continue
            source = _PLATFORM_TO_SOURCE.get(srow["platform"], srow["platform"])
            ledger.upsert_flow(Flow(
                flow_id=sid, source=source,
                status=_LEGACY_STATUS_TO_FLOW.get(srow["status"], srow["status"]),
                mode="observe", started_at=srow["started_at"], ended_at=srow["ended_at"],
                total_tokens=srow["total_tokens"] or 0,
            ))
            summary["flows"] += 1
            steps = src.execute(
                "SELECT * FROM steps WHERE session_id=? ORDER BY timestamp ASC", (sid,)
            ).fetchall()
            task_seq: dict[str, int] = {}
            title_set = False
            for step_seq, st in enumerate(steps):
                legacy_task_id = st["task_id"]
                if legacy_task_id not in task_seq:
                    task_seq[legacy_task_id] = len(task_seq)
                    ledger.upsert_task(Task(
                        task_id=legacy_task_id, flow_id=sid, kind="turn",
                        title=st["task_category"], status="completed",
                        started_at=st["timestamp"], seq=task_seq[legacy_task_id],
                    ))
                    summary["tasks"] += 1
                if not title_set and st["input_summary"]:
                    f = ledger.get_flow(sid)
                    if f is not None:
                        f.title = (st["input_summary"] or "")[:40]
                        ledger.upsert_flow(f)
                    title_set = True
                status = "ok" if st["success"] == 1 else ("failed" if st["success"] == 0 else "running")
                ledger.upsert_step(Step(
                    step_id=st["step_id"], task_id=legacy_task_id, flow_id=sid,
                    tool_name=st["tool_name"], tool_input=st["input_summary"] or None,
                    tool_output=st["output_summary"] or None, status=status,
                    started_at=st["timestamp"], duration_ms=st["latency_ms"],
                    judge_score=st["layer2_score"], seq=step_seq,
                ))
                summary["steps"] += 1
    finally:
        src.close()
    return summary


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None
