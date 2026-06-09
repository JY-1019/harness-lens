"""Persistence layer.

``StorageBackend`` is the abstraction; ``SQLiteStore`` is the default backend.

The five *ledger* tables described in the design (``sessions``, ``steps``,
``evolution_candidates``, ``decision_log``, ``judge_samples``) hold the durable
record. One additional internal table, ``reconstruct_state``, is a per-session
reconstruction *cursor*: because each hook fires in its own short-lived process,
the Flow/Task/Step assembly state cannot live in memory and must be persisted
between events. It is an implementation detail of :mod:`harness_lens.reconstructor`,
not part of the ledger surface.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import home_dir


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _is_daemon_ledger(db_path: Path) -> bool:
    """True if ``db_path`` is a daemon-owned ledger — it carries the Flow/Task/Step ``flows``
    table, whose schema is incompatible with this observe-only store. The legacy paths must
    never open such a file: ``init_schema`` would crash on the mismatched ``steps`` columns."""
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(str(db_path))
        try:
            return con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='flows'"
            ).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def default_db_path(root: Optional[Path] = None) -> Path:
    # The daemon (harness_lens.daemon) owns ``ledger.db`` with the new Flow/Task/Step schema,
    # which is incompatible with this observe-only schema's ``steps`` table. The legacy read
    # paths (show/status/gui via LensService) must therefore never open a daemon-owned DB.
    #
    # When the daemon adopts ``ledger.db`` it relocates any prior observe-only ledger to
    # ``ledger.legacy.db``; prefer that file. But a *daemon-first* install (``install --observe``
    # or a fresh daemon) creates a daemon-schema ``ledger.db`` with no legacy DB to relocate — so
    # whenever ``ledger.db`` carries the daemon schema we route to ``ledger.legacy.db`` regardless
    # (a fresh empty legacy DB is created there on demand). Only a genuine observe-only
    # ``ledger.db`` (or none yet) is returned.
    home = root or home_dir()
    legacy = home / "ledger.legacy.db"
    if legacy.exists():
        return legacy
    primary = home / "ledger.db"
    return legacy if _is_daemon_ledger(primary) else primary


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    session_id: str
    platform: str
    started_at: float
    ended_at: Optional[float] = None
    total_tokens: int = 0
    status: str = "active"


@dataclass
class Step:
    session_id: str
    flow_id: str
    task_id: str
    task_category: str
    tool_name: str
    step_id: str = field(default_factory=lambda: new_id("step"))
    input_summary: str = ""
    output_summary: str = ""
    success: Optional[bool] = None
    latency_ms: Optional[int] = None
    retry_count: int = 0
    layer1_passed: Optional[bool] = None
    layer2_score: Optional[float] = None  # NULL = not evaluated
    # Natural-language harness attribution (design §3 ②-lite). JSON strings, "" = not recorded.
    # layer1_detail: {"checked": [rule, ...], "violations": [{"rule", "detail"}, ...]}
    # layer2_verdicts: [{"criterion_id", "passed", "reason", "weight"}, ...]
    layer1_detail: str = ""
    layer2_verdicts: str = ""
    # False marks a "관측 불가" gap: a step the platform (notably Codex) could not surface
    # through its hooks, so its evidence is incomplete. True for fully observed steps.
    observed: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class EvolutionCandidate:
    failure_pattern: str
    diagnosis: str
    target_component: str
    candidate_id: str = field(default_factory=lambda: new_id("cand"))
    created_at: float = field(default_factory=time.time)
    affected_step_ids: list[str] = field(default_factory=list)
    proposed_change: dict = field(default_factory=dict)
    target_layer: int = 3
    prediction: str = ""
    predicted_metric: str = ""
    predicted_value: Optional[float] = None
    regression_test_result: str = ""
    status: str = "proposed"  # proposed | applied | confirmed | rolled_back | held
    applied_at: Optional[float] = None


@dataclass
class DecisionRecord:
    candidate_id: str
    prediction: str
    predicted_value: Optional[float]
    decision_id: str = field(default_factory=lambda: new_id("dec"))
    actual_value: Optional[float] = None
    verified_at: Optional[float] = None
    was_correct: Optional[bool] = None


@dataclass
class JudgeSample:
    step_id: str
    judge_score: float
    sample_id: str = field(default_factory=lambda: new_id("js"))
    human_label: Optional[float] = None
    agreement: Optional[bool] = None
    reviewed_at: Optional[float] = None


@dataclass
class PromptSnapshot:
    """A copy of an agent-instruction file (CLAUDE.md / AGENTS.md) as it was at session start.

    The hooks cannot read the platform's *assembled* system prompt, but the instruction file is
    its AHE-controllable source (enforce.py writes the 3-Layer block into it), so snapshotting it
    per Flow makes "which system-prompt instructions were in effect" trackable and diffable.
    """
    session_id: str
    scope: str          # '전역' (home) — '프로젝트' reserved for a later build
    path: str
    sha256: str
    line_count: int = 0
    managed_block: bool = False  # whether the enforce-managed 3-Layer block was present
    content: str = ""
    captured_at: float = field(default_factory=time.time)


@dataclass
class Prompt:
    """A user request (UserPromptSubmit) that opened a Task within a Flow.

    The reconstruction cursor only holds the *latest* prompt (``current_task_name``), which the
    next prompt overwrites — so without this durable record the originating request of each Task
    is unrecoverable for display. ``task_id`` is linked once the prompt's first tool step creates
    its Task; it stays ``None`` for a prompt that produced no observed tool call.
    """
    session_id: str
    flow_id: str = ""
    text: str = ""
    task_id: Optional[str] = None
    seq: int = 0
    prompt_id: str = field(default_factory=lambda: new_id("prompt"))
    created_at: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# Backend abstraction
# --------------------------------------------------------------------------- #
class StorageBackend(ABC):
    @abstractmethod
    def init_schema(self) -> None: ...

    # sessions
    @abstractmethod
    def upsert_session(self, session: Session) -> None: ...
    @abstractmethod
    def get_session(self, session_id: str) -> Optional[Session]: ...
    @abstractmethod
    def recent_sessions(self, limit: int = 20, only_failed: bool = False) -> list[Session]: ...

    # steps
    @abstractmethod
    def add_step(self, step: Step) -> Step: ...
    @abstractmethod
    def update_step(self, step: Step) -> None: ...
    @abstractmethod
    def get_step(self, step_id: str) -> Optional[Step]: ...
    @abstractmethod
    def steps_for_session(self, session_id: str) -> list[Step]: ...
    @abstractmethod
    def all_steps(self, since: Optional[float] = None) -> list[Step]: ...

    # evolution candidates
    @abstractmethod
    def add_candidate(self, candidate: EvolutionCandidate) -> EvolutionCandidate: ...
    @abstractmethod
    def update_candidate(self, candidate: EvolutionCandidate) -> None: ...
    @abstractmethod
    def get_candidate(self, candidate_id: str) -> Optional[EvolutionCandidate]: ...
    @abstractmethod
    def list_candidates(self, status: Optional[str] = None) -> list[EvolutionCandidate]: ...

    # decisions
    @abstractmethod
    def add_decision(self, record: DecisionRecord) -> DecisionRecord: ...
    @abstractmethod
    def update_decision(self, record: DecisionRecord) -> None: ...
    @abstractmethod
    def decisions(self) -> list[DecisionRecord]: ...

    # judge samples
    @abstractmethod
    def add_judge_sample(self, sample: JudgeSample) -> JudgeSample: ...
    @abstractmethod
    def update_judge_sample(self, sample: JudgeSample) -> None: ...
    @abstractmethod
    def delete_judge_samples_for_step(self, step_id: str) -> None: ...
    @abstractmethod
    def judge_samples(self, reviewed_only: bool = False) -> list[JudgeSample]: ...

    # prompts (user requests)
    @abstractmethod
    def add_prompt(self, prompt: "Prompt") -> "Prompt": ...
    @abstractmethod
    def link_latest_prompt_to_task(self, session_id: str, task_id: str) -> None: ...
    @abstractmethod
    def prompts_for_session(self, session_id: str) -> list["Prompt"]: ...

    # reconstruction cursor (internal)
    @abstractmethod
    def get_cursor(self, session_id: str) -> dict: ...
    @abstractmethod
    def set_cursor(self, session_id: str, **fields) -> None: ...


# --------------------------------------------------------------------------- #
# SQLite backend
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS steps (
    step_id       TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    flow_id       TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    task_category TEXT NOT NULL,
    tool_name     TEXT NOT NULL,
    input_summary  TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    success       INTEGER,
    latency_ms    INTEGER,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    layer1_passed INTEGER,
    layer2_score  REAL,
    layer1_detail   TEXT NOT NULL DEFAULT '',
    layer2_verdicts TEXT NOT NULL DEFAULT '',
    observed      INTEGER NOT NULL DEFAULT 1,
    timestamp     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_session ON steps(session_id);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id);

CREATE TABLE IF NOT EXISTS evolution_candidates (
    candidate_id          TEXT PRIMARY KEY,
    created_at            REAL NOT NULL,
    failure_pattern       TEXT NOT NULL,
    affected_step_ids     TEXT NOT NULL DEFAULT '[]',
    diagnosis             TEXT NOT NULL DEFAULT '',
    proposed_change       TEXT NOT NULL DEFAULT '{}',
    target_component      TEXT NOT NULL,
    target_layer          INTEGER NOT NULL DEFAULT 3,
    prediction            TEXT NOT NULL DEFAULT '',
    predicted_metric      TEXT NOT NULL DEFAULT '',
    predicted_value       REAL,
    regression_test_result TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'proposed',
    applied_at            REAL
);

CREATE TABLE IF NOT EXISTS decision_log (
    decision_id     TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    prediction      TEXT NOT NULL DEFAULT '',
    predicted_value REAL,
    actual_value    REAL,
    verified_at     REAL,
    was_correct     INTEGER
);

CREATE TABLE IF NOT EXISTS judge_samples (
    sample_id   TEXT PRIMARY KEY,
    step_id     TEXT NOT NULL,
    judge_score REAL NOT NULL,
    human_label REAL,
    agreement   INTEGER,
    reviewed_at REAL
);

CREATE TABLE IF NOT EXISTS prompt_snapshots (
    session_id    TEXT NOT NULL,
    scope         TEXT NOT NULL,
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    line_count    INTEGER NOT NULL DEFAULT 0,
    managed_block INTEGER NOT NULL DEFAULT 0,
    content       TEXT NOT NULL DEFAULT '',
    captured_at   REAL NOT NULL,
    PRIMARY KEY (session_id, scope)
);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    flow_id     TEXT NOT NULL DEFAULT '',
    task_id     TEXT,
    seq         INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);

CREATE TABLE IF NOT EXISTS reconstruct_state (
    session_id        TEXT PRIMARY KEY,
    flow_id           TEXT,
    current_task_id   TEXT,
    current_task_name TEXT,
    current_category  TEXT,
    current_step_id   TEXT,
    pending_task      INTEGER NOT NULL DEFAULT 0,
    last_stop_at      REAL
);
"""


def _b(value: Optional[int]) -> Optional[bool]:
    return None if value is None else bool(value)


class SQLiteStore(StorageBackend):
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for ledgers created by an earlier build.

        ``steps.observed`` was added for the Codex platform (gap tracking); a DB written by
        the Claude-only build lacks the column, so add it with the observed default rather
        than recreating the table.
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(steps)")}
        if "observed" not in cols:
            self._conn.execute("ALTER TABLE steps ADD COLUMN observed INTEGER NOT NULL DEFAULT 1")
        # ②-lite: per-step harness attribution (which invariant/criterion fired and why).
        if "layer1_detail" not in cols:
            self._conn.execute("ALTER TABLE steps ADD COLUMN layer1_detail TEXT NOT NULL DEFAULT ''")
        if "layer2_verdicts" not in cols:
            self._conn.execute("ALTER TABLE steps ADD COLUMN layer2_verdicts TEXT NOT NULL DEFAULT ''")

    # -- sessions -------------------------------------------------------- #
    def upsert_session(self, session: Session) -> None:
        self._conn.execute(
            """INSERT INTO sessions(session_id, platform, started_at, ended_at, total_tokens, status)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 platform=excluded.platform, started_at=excluded.started_at,
                 ended_at=excluded.ended_at, total_tokens=excluded.total_tokens,
                 status=excluded.status""",
            (session.session_id, session.platform, session.started_at,
             session.ended_at, session.total_tokens, session.status),
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> Optional[Session]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return self._session(row) if row else None

    def recent_sessions(self, limit: int = 20, only_failed: bool = False) -> list[Session]:
        sql = "SELECT * FROM sessions"
        params: tuple = ()
        if only_failed:
            sql += " WHERE status='failed'"
        sql += " ORDER BY started_at DESC LIMIT ?"
        params += (limit,)
        return [self._session(r) for r in self._conn.execute(sql, params)]

    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"], platform=row["platform"],
            started_at=row["started_at"], ended_at=row["ended_at"],
            total_tokens=row["total_tokens"], status=row["status"],
        )

    # -- steps ----------------------------------------------------------- #
    def add_step(self, step: Step) -> Step:
        self._conn.execute(
            """INSERT INTO steps(step_id, session_id, flow_id, task_id, task_category,
                   tool_name, input_summary, output_summary, success, latency_ms,
                   retry_count, layer1_passed, layer2_score, layer1_detail, layer2_verdicts,
                   observed, timestamp)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (step.step_id, step.session_id, step.flow_id, step.task_id, step.task_category,
             step.tool_name, step.input_summary, step.output_summary,
             _int(step.success), step.latency_ms, step.retry_count,
             _int(step.layer1_passed), step.layer2_score, step.layer1_detail, step.layer2_verdicts,
             _int(step.observed), step.timestamp),
        )
        self._conn.commit()
        return step

    def update_step(self, step: Step) -> None:
        self._conn.execute(
            """UPDATE steps SET task_category=?, tool_name=?, input_summary=?,
                   output_summary=?, success=?, latency_ms=?, retry_count=?,
                   layer1_passed=?, layer2_score=?, layer1_detail=?, layer2_verdicts=?,
                   observed=? WHERE step_id=?""",
            (step.task_category, step.tool_name, step.input_summary, step.output_summary,
             _int(step.success), step.latency_ms, step.retry_count,
             _int(step.layer1_passed), step.layer2_score, step.layer1_detail, step.layer2_verdicts,
             _int(step.observed), step.step_id),
        )
        self._conn.commit()

    def get_step(self, step_id: str) -> Optional[Step]:
        row = self._conn.execute("SELECT * FROM steps WHERE step_id=?", (step_id,)).fetchone()
        return self._step(row) if row else None

    def steps_for_session(self, session_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE session_id=? ORDER BY timestamp ASC", (session_id,)
        )
        return [self._step(r) for r in rows]

    def all_steps(self, since: Optional[float] = None) -> list[Step]:
        if since is None:
            rows = self._conn.execute("SELECT * FROM steps ORDER BY timestamp ASC")
        else:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE timestamp>=? ORDER BY timestamp ASC", (since,)
            )
        return [self._step(r) for r in rows]

    @staticmethod
    def _step(row: sqlite3.Row) -> Step:
        return Step(
            step_id=row["step_id"], session_id=row["session_id"], flow_id=row["flow_id"],
            task_id=row["task_id"], task_category=row["task_category"], tool_name=row["tool_name"],
            input_summary=row["input_summary"], output_summary=row["output_summary"],
            success=_b(row["success"]), latency_ms=row["latency_ms"], retry_count=row["retry_count"],
            layer1_passed=_b(row["layer1_passed"]), layer2_score=row["layer2_score"],
            layer1_detail=row["layer1_detail"] if "layer1_detail" in row.keys() else "",
            layer2_verdicts=row["layer2_verdicts"] if "layer2_verdicts" in row.keys() else "",
            observed=_b(row["observed"]) if "observed" in row.keys() else True,
            timestamp=row["timestamp"],
        )

    # -- evolution candidates ------------------------------------------- #
    def add_candidate(self, candidate: EvolutionCandidate) -> EvolutionCandidate:
        self._conn.execute(
            """INSERT INTO evolution_candidates(candidate_id, created_at, failure_pattern,
                   affected_step_ids, diagnosis, proposed_change, target_component, target_layer,
                   prediction, predicted_metric, predicted_value, regression_test_result, status, applied_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate.candidate_id, candidate.created_at, candidate.failure_pattern,
             json.dumps(candidate.affected_step_ids), candidate.diagnosis,
             json.dumps(candidate.proposed_change), candidate.target_component, candidate.target_layer,
             candidate.prediction, candidate.predicted_metric, candidate.predicted_value,
             candidate.regression_test_result, candidate.status, candidate.applied_at),
        )
        self._conn.commit()
        return candidate

    def update_candidate(self, candidate: EvolutionCandidate) -> None:
        self._conn.execute(
            """UPDATE evolution_candidates SET failure_pattern=?, affected_step_ids=?, diagnosis=?,
                   proposed_change=?, target_component=?, target_layer=?, prediction=?,
                   predicted_metric=?, predicted_value=?, regression_test_result=?, status=?, applied_at=?
               WHERE candidate_id=?""",
            (candidate.failure_pattern, json.dumps(candidate.affected_step_ids), candidate.diagnosis,
             json.dumps(candidate.proposed_change), candidate.target_component, candidate.target_layer,
             candidate.prediction, candidate.predicted_metric, candidate.predicted_value,
             candidate.regression_test_result, candidate.status, candidate.applied_at,
             candidate.candidate_id),
        )
        self._conn.commit()

    def get_candidate(self, candidate_id: str) -> Optional[EvolutionCandidate]:
        row = self._conn.execute(
            "SELECT * FROM evolution_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        return self._candidate(row) if row else None

    def list_candidates(self, status: Optional[str] = None) -> list[EvolutionCandidate]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM evolution_candidates WHERE status=? ORDER BY created_at DESC", (status,)
            )
        else:
            rows = self._conn.execute("SELECT * FROM evolution_candidates ORDER BY created_at DESC")
        return [self._candidate(r) for r in rows]

    @staticmethod
    def _candidate(row: sqlite3.Row) -> EvolutionCandidate:
        return EvolutionCandidate(
            candidate_id=row["candidate_id"], created_at=row["created_at"],
            failure_pattern=row["failure_pattern"],
            affected_step_ids=json.loads(row["affected_step_ids"]),
            diagnosis=row["diagnosis"], proposed_change=json.loads(row["proposed_change"]),
            target_component=row["target_component"], target_layer=row["target_layer"],
            prediction=row["prediction"], predicted_metric=row["predicted_metric"],
            predicted_value=row["predicted_value"], regression_test_result=row["regression_test_result"],
            status=row["status"], applied_at=row["applied_at"],
        )

    # -- decisions ------------------------------------------------------- #
    def add_decision(self, record: DecisionRecord) -> DecisionRecord:
        self._conn.execute(
            """INSERT INTO decision_log(decision_id, candidate_id, prediction, predicted_value,
                   actual_value, verified_at, was_correct) VALUES(?,?,?,?,?,?,?)""",
            (record.decision_id, record.candidate_id, record.prediction, record.predicted_value,
             record.actual_value, record.verified_at, _int(record.was_correct)),
        )
        self._conn.commit()
        return record

    def update_decision(self, record: DecisionRecord) -> None:
        self._conn.execute(
            """UPDATE decision_log SET prediction=?, predicted_value=?, actual_value=?,
                   verified_at=?, was_correct=? WHERE decision_id=?""",
            (record.prediction, record.predicted_value, record.actual_value,
             record.verified_at, _int(record.was_correct), record.decision_id),
        )
        self._conn.commit()

    def decisions(self) -> list[DecisionRecord]:
        rows = self._conn.execute("SELECT * FROM decision_log ORDER BY verified_at IS NULL DESC, verified_at DESC")
        return [
            DecisionRecord(
                decision_id=r["decision_id"], candidate_id=r["candidate_id"], prediction=r["prediction"],
                predicted_value=r["predicted_value"], actual_value=r["actual_value"],
                verified_at=r["verified_at"], was_correct=_b(r["was_correct"]),
            )
            for r in rows
        ]

    # -- judge samples --------------------------------------------------- #
    def add_judge_sample(self, sample: JudgeSample) -> JudgeSample:
        self._conn.execute(
            """INSERT INTO judge_samples(sample_id, step_id, judge_score, human_label, agreement, reviewed_at)
               VALUES(?,?,?,?,?,?)""",
            (sample.sample_id, sample.step_id, sample.judge_score, sample.human_label,
             _int(sample.agreement), sample.reviewed_at),
        )
        self._conn.commit()
        return sample

    def update_judge_sample(self, sample: JudgeSample) -> None:
        self._conn.execute(
            """UPDATE judge_samples SET judge_score=?, human_label=?, agreement=?, reviewed_at=?
               WHERE sample_id=?""",
            (sample.judge_score, sample.human_label, _int(sample.agreement),
             sample.reviewed_at, sample.sample_id),
        )
        self._conn.commit()

    def delete_judge_samples_for_step(self, step_id: str) -> None:
        self._conn.execute("DELETE FROM judge_samples WHERE step_id=?", (step_id,))
        self._conn.commit()

    def judge_samples(self, reviewed_only: bool = False) -> list[JudgeSample]:
        sql = "SELECT * FROM judge_samples"
        if reviewed_only:
            sql += " WHERE reviewed_at IS NOT NULL"
        sql += " ORDER BY reviewed_at IS NULL ASC, reviewed_at DESC"
        return [
            JudgeSample(
                sample_id=r["sample_id"], step_id=r["step_id"], judge_score=r["judge_score"],
                human_label=r["human_label"], agreement=_b(r["agreement"]), reviewed_at=r["reviewed_at"],
            )
            for r in self._conn.execute(sql)
        ]

    # -- prompt snapshots ------------------------------------------------ #
    def add_prompt_snapshot(self, snap: PromptSnapshot) -> PromptSnapshot:
        self._conn.execute(
            """INSERT INTO prompt_snapshots(session_id, scope, path, sha256, line_count,
                   managed_block, content, captured_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id, scope) DO UPDATE SET
                 path=excluded.path, sha256=excluded.sha256, line_count=excluded.line_count,
                 managed_block=excluded.managed_block, content=excluded.content,
                 captured_at=excluded.captured_at""",
            (snap.session_id, snap.scope, snap.path, snap.sha256, snap.line_count,
             _int(snap.managed_block), snap.content, snap.captured_at),
        )
        self._conn.commit()
        return snap

    def prompt_snapshots_for_session(self, session_id: str) -> list[PromptSnapshot]:
        return [
            PromptSnapshot(
                session_id=r["session_id"], scope=r["scope"], path=r["path"], sha256=r["sha256"],
                line_count=r["line_count"], managed_block=bool(r["managed_block"]),
                content=r["content"], captured_at=r["captured_at"],
            )
            for r in self._conn.execute(
                "SELECT * FROM prompt_snapshots WHERE session_id=? ORDER BY scope", (session_id,)
            )
        ]

    # -- prompts (user requests) ---------------------------------------- #
    def add_prompt(self, prompt: "Prompt") -> "Prompt":
        # Assign a monotonic per-session sequence so requests render in submission order even
        # when two prompts land in the same wall-clock instant.
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM prompts WHERE session_id=?", (prompt.session_id,)
        ).fetchone()
        prompt.seq = int(row[0])
        self._conn.execute(
            """INSERT INTO prompts(prompt_id, session_id, flow_id, task_id, seq, text, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (prompt.prompt_id, prompt.session_id, prompt.flow_id, prompt.task_id,
             prompt.seq, prompt.text, prompt.created_at),
        )
        self._conn.commit()
        return prompt

    def link_latest_prompt_to_task(self, session_id: str, task_id: str) -> None:
        # The most recent still-unlinked prompt is the one whose first tool call just opened
        # this Task; bind them so the GUI can show each Task's originating request.
        self._conn.execute(
            """UPDATE prompts SET task_id=?
               WHERE prompt_id = (
                   SELECT prompt_id FROM prompts
                   WHERE session_id=? AND task_id IS NULL
                   ORDER BY seq DESC LIMIT 1
               )""",
            (task_id, session_id),
        )
        self._conn.commit()

    def prompts_for_session(self, session_id: str) -> list["Prompt"]:
        return [
            Prompt(
                prompt_id=r["prompt_id"], session_id=r["session_id"], flow_id=r["flow_id"],
                task_id=r["task_id"], seq=r["seq"], text=r["text"], created_at=r["created_at"],
            )
            for r in self._conn.execute(
                "SELECT * FROM prompts WHERE session_id=? ORDER BY seq ASC", (session_id,)
            )
        ]

    # -- reconstruction cursor ------------------------------------------ #
    _CURSOR_FIELDS = (
        "flow_id", "current_task_id", "current_task_name",
        "current_category", "current_step_id", "pending_task", "last_stop_at",
    )
    # Schema defaults for a freshly seeded cursor. ``pending_task`` is NOT NULL, so a
    # partial update (e.g. an out-of-order on_stop before SessionStart) must not write
    # None into it.
    _CURSOR_DEFAULTS = {"pending_task": 0}

    def get_cursor(self, session_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM reconstruct_state WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            return {}
        return {k: row[k] for k in self._CURSOR_FIELDS}

    def set_cursor(self, session_id: str, **fields) -> None:
        current = self.get_cursor(session_id)
        if not current:
            self._conn.execute(
                "INSERT INTO reconstruct_state(session_id) VALUES(?)", (session_id,)
            )
            current = {k: self._CURSOR_DEFAULTS.get(k) for k in self._CURSOR_FIELDS}
        merged = {**current, **{k: v for k, v in fields.items() if k in self._CURSOR_FIELDS}}
        self._conn.execute(
            f"""UPDATE reconstruct_state SET {', '.join(f'{k}=?' for k in self._CURSOR_FIELDS)}
                WHERE session_id=?""",
            tuple(merged[k] for k in self._CURSOR_FIELDS) + (session_id,),
        )
        self._conn.commit()


def _int(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(value)
