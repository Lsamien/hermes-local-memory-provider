from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .prompts import summary_prompt_template

logger = logging.getLogger(__name__)


class ReflectorWorker:
    """Independent local reflector worker.

    Stores events + periodic summaries into a sidecar SQLite database.
    """

    def __init__(
        self,
        sqlite_path: str,
        trigger_every_n_turns: int = 8,
        async_mode: bool = True,
    ):
        self.sqlite_path = Path(sqlite_path).expanduser().resolve()
        self.trigger_every_n_turns = max(1, int(trigger_every_n_turns))
        self.async_mode = bool(async_mode)

    def initialize(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_message TEXT,
                    assistant_message TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_count INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    prompt_used TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Backfill cleanup: keep the newest summary for each (session_id, turn_count)
            # so we can enforce idempotency with a unique index.
            conn.execute(
                """
                DELETE FROM memory_summaries
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM memory_summaries
                    GROUP BY session_id, turn_count
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_summaries_session_turn
                ON memory_summaries(session_id, turn_count)
                """
            )
            conn.commit()

    def observe_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        if self.async_mode:
            t = threading.Thread(
                target=self._observe_turn_sync,
                args=(session_id, user_message, assistant_message, metadata or {}),
                daemon=True,
                name="local-memory-reflector",
            )
            t.start()
            return

        self._observe_turn_sync(session_id, user_message, assistant_message, metadata or {})

    def on_session_end(self, session_id: str) -> None:
        try:
            self._write_summary_for_latest(session_id, force=True)
        except Exception as exc:
            logger.warning("Reflector on_session_end failed: %s", exc)

    def get_recent_summaries(self, session_id: str, limit: int = 2) -> List[str]:
        limit = max(1, min(int(limit), 10))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT summary FROM memory_summaries
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]

    def _observe_turn_sync(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_events
                (session_id, user_message, assistant_message, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_message,
                    assistant_message,
                    json.dumps(metadata, ensure_ascii=False),
                    self._now_iso(),
                ),
            )
            conn.commit()

        self._write_summary_for_latest(session_id, force=False)

    def _write_summary_for_latest(self, session_id: str, force: bool) -> None:
        with self._connect() as conn:
            total_turns = conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]

            if total_turns <= 0:
                return
            if not force and total_turns % self.trigger_every_n_turns != 0:
                return

            rows = conn.execute(
                """
                SELECT user_message, assistant_message
                FROM memory_events
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, self.trigger_every_n_turns),
            ).fetchall()

            pairs = list(reversed(rows))
            summary_lines: List[str] = []
            for idx, (u, a) in enumerate(pairs, start=1):
                us = (u or "").strip().replace("\n", " ")
                ass = (a or "").strip().replace("\n", " ")
                if len(us) > 160:
                    us = us[:160] + "..."
                if len(ass) > 160:
                    ass = ass[:160] + "..."
                summary_lines.append(f"{idx}. U:{us} | A:{ass}")

            summary = "\n".join(summary_lines)
            prompt_used = summary_prompt_template(session_id=session_id, turn_count=total_turns)

            conn.execute(
                """
                INSERT INTO memory_summaries
                (session_id, turn_count, summary, prompt_used, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, turn_count) DO UPDATE SET
                    summary=excluded.summary,
                    prompt_used=excluded.prompt_used,
                    created_at=excluded.created_at
                """,
                (session_id, total_turns, summary, prompt_used, self._now_iso()),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.sqlite_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
