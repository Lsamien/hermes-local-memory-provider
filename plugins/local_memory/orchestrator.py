from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compatibility_adapter import HermesSQLiteCompatibilityAdapter

logger = logging.getLogger(__name__)


class MemoryOrchestrator:
    """Business logic for local memory ingestion and recall.

    Provider layer should only delegate lifecycle calls here.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        hermes_cfg = config.get("hermes", {})
        schema_cfg = config.get("hermes_schema", {})
        memory_cfg = config.get("memory_index", {})

        self.adapter = HermesSQLiteCompatibilityAdapter(
            sqlite_path=str(hermes_cfg.get("sqlite_path", "")),
            schema_config=schema_cfg,
        )
        self.memory_index_path = Path(
            str(memory_cfg.get("sqlite_path", "memory/local_memory/memory_index.sqlite"))
        ).expanduser().resolve()

        recall_cfg = config.get("recall", {})
        self.max_results = int(recall_cfg.get("max_results", 8))

        fts_cfg = config.get("fts", {})
        self.fts_enabled = bool(fts_cfg.get("enabled", True))

        graphiti_cfg = config.get("graphiti", {})
        self.graphiti_enabled = bool(graphiti_cfg.get("enabled", False))
        self._graphiti_adapter: Optional[Any] = None
        self._graphiti_sync_worker: Optional[Any] = None
        self._graphiti_recall_max_results = int(graphiti_cfg.get("recall_max_results", 5))

        reflector_cfg = config.get("reflector", {})
        self.reflector_enabled = bool(reflector_cfg.get("enabled", False))
        self._reflector_worker: Optional[Any] = None
        self._reflector_prefetch_summaries = int(reflector_cfg.get("prefetch_summaries", 2))

        nsfw_cfg = config.get("nsfw", {})
        self.nsfw_save = bool(nsfw_cfg.get("save", True))
        self.nsfw_index = bool(nsfw_cfg.get("index", True))
        self.nsfw_default_recall = bool(nsfw_cfg.get("default_recall", False))
        self.nsfw_allow_explicit_history_search = bool(
            nsfw_cfg.get("allow_explicit_history_search", True)
        )
        self._nsfw_keywords = self._build_nsfw_keywords(nsfw_cfg)
        self._nsfw_query_keywords = self._build_nsfw_query_keywords(nsfw_cfg, self._nsfw_keywords)

    def initialize(self) -> None:
        self.adapter.validate_schema()
        self.memory_index_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect_index() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_source_message_id TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_message_id TEXT NOT NULL UNIQUE,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    created_at TEXT,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            self._ensure_memory_turns_columns(conn)
            self._backfill_nsfw_tags(conn)
            if self.fts_enabled:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_turns_fts
                    USING fts5(content, session_id UNINDEXED, role UNINDEXED)
                    """
                )
            conn.commit()

        if self.graphiti_enabled:
            try:
                from memory.graphiti.adapter import GraphitiAdapter, GraphitiConfig
                from memory.graphiti.sync_worker import GraphitiSyncWorker

                graphiti_cfg = self.config.get("graphiti", {})
                adapter = GraphitiAdapter(
                    GraphitiConfig(
                        endpoint=str(graphiti_cfg.get("endpoint", "http://localhost:18000/mcp")),
                        group_id=str(graphiti_cfg.get("group_id", "zhuer")),
                        timeout_ms=int(graphiti_cfg.get("timeout_ms", 600)),
                        recall_max_results=self._graphiti_recall_max_results,
                    )
                )
                ok, detail = adapter.ping()
                if ok:
                    self._graphiti_adapter = adapter
                    self._graphiti_sync_worker = GraphitiSyncWorker(
                        adapter=adapter,
                        async_write=bool(graphiti_cfg.get("async_write", True)),
                    )
                else:
                    logger.warning("Graphiti adapter disabled (unreachable): %s", detail)
            except Exception as exc:
                logger.warning("Graphiti adapter init failed (non-fatal): %s", exc)

        if self.reflector_enabled:
            try:
                from memory.reflector.worker import ReflectorWorker

                reflector_cfg = self.config.get("reflector", {})
                sqlite_path = str(
                    reflector_cfg.get(
                        "sqlite_path",
                        str(self.memory_index_path.parent / "reflector.sqlite"),
                    )
                )
                worker = ReflectorWorker(
                    sqlite_path=sqlite_path,
                    trigger_every_n_turns=int(reflector_cfg.get("trigger_every_n_turns", 8)),
                    async_mode=bool(reflector_cfg.get("async", True)),
                )
                worker.initialize()
                self._reflector_worker = worker
            except Exception as exc:
                logger.warning("Reflector worker init failed (non-fatal): %s", exc)

    def prefetch(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        if not query or not query.strip():
            return ""

        session_id = ""
        if context and context.get("session_id"):
            session_id = str(context.get("session_id"))

        try:
            rows = self._search(query=query, session_id=session_id, limit=self.max_results)
        except Exception as exc:
            logger.warning("local_memory prefetch search failed: %s", exc)
            return ""

        sections: List[str] = []

        if rows:
            lines = ["# Local Memory Recall"]
            for row in rows:
                if self._should_hide_from_default_recall(row, query):
                    continue
                role = row.get("role", "")
                ts = row.get("created_at", "")
                sid = row.get("session_id", "")
                text = row.get("content", "").strip().replace("\n", " ")
                if len(text) > 220:
                    text = text[:220] + "..."
                lines.append(f"- [{ts}] ({sid}) {role}: {text}")
            if len(lines) > 1:
                sections.append("\n".join(lines))

        if self._graphiti_adapter:
            try:
                g_lines = self._graphiti_adapter.recall(
                    query=query,
                    max_results=self._graphiti_recall_max_results,
                )
                if g_lines:
                    sections.append(
                        "# Graphiti Recall\n" + "\n".join(f"- {item}" for item in g_lines)
                    )
            except Exception as exc:
                logger.warning("Graphiti recall failed (non-fatal): %s", exc)

        if self._reflector_worker and session_id:
            try:
                summaries = self._reflector_worker.get_recent_summaries(
                    session_id=session_id,
                    limit=self._reflector_prefetch_summaries,
                )
                if summaries:
                    sections.append(
                        "# Reflector Summaries\n"
                        + "\n".join(f"- {s.replace(chr(10), ' ')}" for s in summaries)
                    )
            except Exception as exc:
                logger.warning("Reflector prefetch failed (non-fatal): %s", exc)

        return "\n\n".join(sections)

    def sync_turn(
        self,
        user_message: str,
        assistant_message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sync_stats = self._ingest_new_messages(limit=500)
        session_id = str((metadata or {}).get("session_id", ""))

        if self._graphiti_sync_worker:
            try:
                self._graphiti_sync_worker.sync_turn(
                    session_id=session_id or "unknown",
                    user_message=user_message,
                    assistant_message=assistant_message,
                    metadata=metadata or {},
                )
            except Exception as exc:
                logger.warning("Graphiti sync dispatch failed (non-fatal): %s", exc)

        if self._reflector_worker:
            try:
                self._reflector_worker.observe_turn(
                    session_id=session_id or "unknown",
                    user_message=user_message,
                    assistant_message=assistant_message,
                    metadata=metadata or {},
                )
            except Exception as exc:
                logger.warning("Reflector observe_turn failed (non-fatal): %s", exc)

        return sync_stats

    def on_session_end(self, session_id: str) -> Dict[str, Any]:
        stats = self._ingest_new_messages(limit=1000)
        if self._reflector_worker:
            try:
                self._reflector_worker.on_session_end(session_id=session_id or "unknown")
            except Exception as exc:
                logger.warning("Reflector on_session_end failed (non-fatal): %s", exc)
        return stats

    def _ingest_new_messages(self, limit: int) -> Dict[str, Any]:
        with self._connect_index() as conn:
            last_id = self._get_last_source_message_id(conn)
            new_rows = self.adapter.fetch_new_messages(since_source_message_id=last_id, limit=limit)

            inserted = 0
            for row in new_rows:
                if self.nsfw_save:
                    row_nsfw_tag, row_nsfw_reason = self._classify_nsfw(row.get("content", ""))
                else:
                    row_nsfw_tag, row_nsfw_reason = 0, ""
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_turns
                    (source_message_id, session_id, role, content, created_at, indexed_at, nsfw_tag, nsfw_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("source_message_id", ""),
                        row.get("session_id", ""),
                        row.get("role", ""),
                        row.get("content", ""),
                        row.get("created_at", ""),
                        self._now_iso(),
                        row_nsfw_tag,
                        row_nsfw_reason,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                    should_index = self.fts_enabled and (self.nsfw_index or row_nsfw_tag == 0)
                    if should_index:
                        conn.execute(
                            """
                            INSERT INTO memory_turns_fts (rowid, content, session_id, role)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                cur.lastrowid,
                                row.get("content", ""),
                                row.get("session_id", ""),
                                row.get("role", ""),
                            ),
                        )

            if new_rows:
                newest = str(new_rows[-1].get("source_message_id", ""))
                conn.execute(
                    """
                    INSERT INTO sync_state (id, last_source_message_id, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        last_source_message_id=excluded.last_source_message_id,
                        updated_at=excluded.updated_at
                    """,
                    (newest, self._now_iso()),
                )
            conn.commit()

        return {
            "fetched": len(new_rows),
            "inserted": inserted,
            "last_source_message_id": new_rows[-1]["source_message_id"] if new_rows else last_id,
        }

    def _search(self, query: str, session_id: str, limit: int) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 50))

        with self._connect_index() as conn:
            if self.fts_enabled:
                sql = (
                    """
                    SELECT t.session_id, t.role, t.content, t.created_at, t.nsfw_tag, t.nsfw_reason
                    FROM memory_turns_fts f
                    JOIN memory_turns t ON t.id = f.rowid
                    WHERE f.content MATCH ?
                    """
                )
                params: List[Any] = [query]
                if session_id:
                    sql += " AND t.session_id = ?"
                    params.append(session_id)
                sql += " ORDER BY t.id DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()
                if rows:
                    return [
                        {
                            "session_id": r[0] or "",
                            "role": r[1] or "",
                            "content": r[2] or "",
                            "created_at": r[3] or "",
                            "nsfw_tag": int(r[4] or 0),
                            "nsfw_reason": r[5] or "",
                        }
                        for r in rows
                    ]

            sql = (
                """
                SELECT session_id, role, content, created_at, nsfw_tag, nsfw_reason
                FROM memory_turns
                WHERE content LIKE ?
                """
            )
            params = [f"%{query}%"]
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [
                {
                    "session_id": r[0] or "",
                    "role": r[1] or "",
                    "content": r[2] or "",
                    "created_at": r[3] or "",
                    "nsfw_tag": int(r[4] or 0),
                    "nsfw_reason": r[5] or "",
                }
                for r in rows
            ]

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _get_last_source_message_id(self, conn: sqlite3.Connection) -> Optional[str]:
        row = conn.execute(
            "SELECT last_source_message_id FROM sync_state WHERE id = 1"
        ).fetchone()
        if row and row[0] is not None and str(row[0]).strip():
            return str(row[0])
        return None

    def _connect_index(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.memory_index_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _ensure_memory_turns_columns(conn: sqlite3.Connection) -> None:
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(memory_turns)").fetchall()
        }
        if "nsfw_tag" not in cols:
            conn.execute(
                "ALTER TABLE memory_turns ADD COLUMN nsfw_tag INTEGER NOT NULL DEFAULT 0"
            )
        if "nsfw_reason" not in cols:
            conn.execute(
                "ALTER TABLE memory_turns ADD COLUMN nsfw_reason TEXT"
            )

    def _backfill_nsfw_tags(self, conn: sqlite3.Connection) -> None:
        # Keep existing memory intact, but backfill configurable NSFW tags on legacy rows.
        if not self.nsfw_save:
            return
        for kw in self._nsfw_keywords:
            conn.execute(
                """
                UPDATE memory_turns
                SET nsfw_tag = 1, nsfw_reason = COALESCE(NULLIF(nsfw_reason, ''), ?)
                WHERE nsfw_tag = 0
                  AND instr(lower(content), lower(?)) > 0
                """,
                (f"keyword:{kw}", kw),
            )

    def _classify_nsfw(self, content: Any) -> tuple[int, str]:
        text = str(content or "").lower()
        if not text.strip():
            return 0, ""
        for kw in self._nsfw_keywords:
            if kw in text:
                return 1, f"keyword:{kw}"
        return 0, ""

    def _should_hide_from_default_recall(self, row: Dict[str, Any], query: str) -> bool:
        if self.nsfw_default_recall:
            return False
        if int(row.get("nsfw_tag", 0) or 0) <= 0:
            return False
        if not self.nsfw_allow_explicit_history_search:
            return True
        return not self._is_explicit_query(query)

    def _is_explicit_query(self, query: str) -> bool:
        text = str(query or "").lower()
        return any(tok in text for tok in self._nsfw_query_keywords)

    @staticmethod
    def _normalize_keyword_list(items: Any) -> List[str]:
        if not isinstance(items, list):
            return []
        out: List[str] = []
        seen = set()
        for item in items:
            kw = str(item or "").strip().lower()
            if not kw or kw in seen:
                continue
            seen.add(kw)
            out.append(kw)
        return out

    @classmethod
    def _build_nsfw_keywords(cls, nsfw_cfg: Dict[str, Any]) -> List[str]:
        defaults_en = [
            "nsfw",
            "18+",
            "adult",
            "explicit",
            "erotic",
            "sex",
            "sexual",
            "sexy",
            "nude",
            "nudity",
            "porn",
            "xxx",
            "fetish",
            "kink",
        ]
        defaults_zh = [
            "成人",
            "露骨",
            "色情",
            "性爱",
            "性交",
            "做爱",
            "裸体",
            "无码",
            "有码",
            "黄片",
            "成人视频",
            "约炮",
            "调教",
            "性癖",
        ]
        cfg_en = cls._normalize_keyword_list(nsfw_cfg.get("keywords_en", []))
        cfg_zh = cls._normalize_keyword_list(nsfw_cfg.get("keywords_zh", []))
        cfg_extra = cls._normalize_keyword_list(nsfw_cfg.get("extra_keywords", []))
        combined = cfg_en + cfg_zh + cfg_extra
        if not combined:
            combined = defaults_en + defaults_zh
        return combined

    @classmethod
    def _build_nsfw_query_keywords(
        cls,
        nsfw_cfg: Dict[str, Any],
        base_keywords: List[str],
    ) -> List[str]:
        defaults = [
            "nsfw",
            "adult",
            "explicit",
            "erotic",
            "porn",
            "xxx",
            "成人",
            "色情",
            "露骨",
            "性爱",
        ]
        query_only = cls._normalize_keyword_list(nsfw_cfg.get("query_keywords", []))
        merged = base_keywords + query_only + defaults
        # Keep stable order + de-dup.
        seen = set()
        out: List[str] = []
        for kw in merged:
            if kw in seen:
                continue
            seen.add(kw)
            out.append(kw)
        return out
