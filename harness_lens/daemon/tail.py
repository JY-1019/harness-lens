"""JSONL tail — the auxiliary enrichment channel (design §7).

Hooks deliver control + structure but miss some fields (token usage, assistant message
bodies). This watches the two CLIs' transcript directories and back-fills *only* fields the
hook payloads lack, deduped against hook-sourced data by ``(session_id, tool_use_id)``.

It is strictly best-effort and **must never crash the daemon**: a permission error, an AV
quarantine, a malformed line, or a missing directory is logged-and-ignored. It is enrichment,
never a source of truth — it does not create Flows/Steps, only augments existing ones.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

# Claude Code transcripts: ~/.claude/projects/**/*.jsonl
# Codex rollouts:          ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
_CLAUDE_GLOB_ROOT = Path.home() / ".claude" / "projects"
_CODEX_GLOB_ROOT = Path.home() / ".codex" / "sessions"


class _OffsetReader:
    """Remember how far we have read each file so a modify event only yields new lines."""

    def __init__(self) -> None:
        self._offsets: dict[str, int] = {}

    def read_new(self, path: Path) -> list[dict]:
        key = str(path)
        records: list[dict] = []
        try:
            size = path.stat().st_size
            start = self._offsets.get(key, 0)
            if size < start:  # file truncated/rotated — restart from the top
                start = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(start)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                self._offsets[key] = fh.tell()
        except OSError:
            # Permission denied / quarantined / vanished — ignore this file, keep the daemon alive.
            return []
        return records


class JsonlTail:
    """Watch the transcript dirs and call ``on_record(source, record)`` for each new line.

    Runs a watchdog observer on a background thread. ``start``/``stop`` are no-ops if watchdog
    is unavailable, so the daemon degrades gracefully to hooks-only when the extra is missing.
    """

    def __init__(self, on_record: Callable[[str, dict], None]):
        self._on_record = on_record
        self._reader = _OffsetReader()
        self._observer = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:  # noqa: BLE001 — watchdog not installed → tail simply disabled
            return False

        tail = self

        class _Handler(FileSystemEventHandler):
            def __init__(self, source: str):
                self.source = source

            def on_modified(self, event):
                self._feed(event)

            def on_created(self, event):
                self._feed(event)

            def _feed(self, event):
                if getattr(event, "is_directory", False):
                    return
                path = Path(event.src_path)
                if path.suffix != ".jsonl":
                    return
                with tail._lock:
                    for record in tail._reader.read_new(path):
                        try:
                            tail._on_record(self.source, record)
                        except Exception:  # noqa: BLE001 — one bad record never stops the tail
                            continue

        observer = Observer()
        for root, source in ((_CLAUDE_GLOB_ROOT, "claude_code"), (_CODEX_GLOB_ROOT, "codex")):
            if root.is_dir():
                observer.schedule(_Handler(source), str(root), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        return True

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None


def enrich_tokens(record: dict) -> Optional[tuple[str, int]]:
    """Extract ``(session_id, total_tokens)`` from a transcript record, if present.

    Both CLIs record token usage in their transcripts but not always in hook payloads; this is
    the canonical example of a hook-missing field the tail back-fills. Returns None when the
    record carries no usage we recognise.
    """
    session_id = record.get("session_id") or record.get("sessionId")
    usage = record.get("usage") or (record.get("message") or {}).get("usage")
    if not session_id or not isinstance(usage, dict):
        return None
    total = 0
    for key in ("input_tokens", "output_tokens", "total_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    if total <= 0:
        return None
    return str(session_id), total
