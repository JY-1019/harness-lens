"""Regression: the observe-only ``LensService`` / ``SQLiteStore`` must never open a
daemon-owned ``ledger.db`` (the new Flow/Task/Step schema).

Before the fix, once the daemon had created its ``ledger.db`` every ``_service()``-based CLI
command (``status`` / ``diagnose`` / ``evolve`` / ``verify`` / ``review`` / ``rollback`` /
``harness``) crashed with ``sqlite3.OperationalError: no such column: session_id`` because
``init_schema`` ran the legacy schema against the daemon's incompatible tables.
``default_db_path()`` now routes legacy reads to ``ledger.legacy.db`` whenever ``ledger.db``
carries the daemon schema — even in a daemon-first install with no relocated legacy DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harness_lens.service import LensService
from harness_lens.store import default_db_path


def _make_daemon_ledger(path: Path) -> None:
    """A minimal daemon-owned ledger — only the marker ``flows`` table matters here."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE flows (flow_id TEXT PRIMARY KEY)")
        con.commit()
    finally:
        con.close()


def _make_legacy_ledger(path: Path) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        con.commit()
    finally:
        con.close()


def test_daemon_ledger_routes_legacy_reads_aside(tmp_home):
    _make_daemon_ledger(tmp_home / "ledger.db")
    # ledger.db is daemon-owned → legacy reads must go to ledger.legacy.db, never the daemon DB.
    assert default_db_path(tmp_home) == tmp_home / "ledger.legacy.db"


def test_plain_legacy_ledger_is_used_as_is(tmp_home):
    _make_legacy_ledger(tmp_home / "ledger.db")
    # A genuine observe-only ledger.db (no flows table) is returned unchanged.
    assert default_db_path(tmp_home) == tmp_home / "ledger.db"


def test_existing_legacy_db_is_preferred(tmp_home):
    (tmp_home / "ledger.legacy.db").write_bytes(b"")
    _make_daemon_ledger(tmp_home / "ledger.db")
    assert default_db_path(tmp_home) == tmp_home / "ledger.legacy.db"


def test_fresh_home_uses_canonical_path(tmp_home):
    # Nothing created yet → the canonical ledger.db path (a fresh legacy store is created there).
    assert default_db_path(tmp_home) == tmp_home / "ledger.db"


def test_lensservice_status_survives_daemon_ledger(tmp_home):
    _make_daemon_ledger(tmp_home / "ledger.db")
    # Before the fix this raised sqlite3.OperationalError during SQLiteStore.init_schema.
    service = LensService(root=tmp_home)
    try:
        status = service.status()
    finally:
        service.close()
    assert {"judge", "layer1", "layer2", "layer3", "candidates"} <= set(status)
    # It opened its own (fresh, empty) legacy DB, not the daemon ledger.
    assert service.store.db_path == tmp_home / "ledger.legacy.db"
