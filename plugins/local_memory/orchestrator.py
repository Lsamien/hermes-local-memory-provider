from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
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

        ingest_cfg = config.get("ingest", {})
        allowed_roles = ingest_cfg.get("allowed_roles", ["user", "assistant"])
        self._ingest_allowed_roles = {
            str(role).strip().lower() for role in allowed_roles if str(role).strip()
        }
        self._ingest_drop_skill_invoke_banner = bool(
            ingest_cfg.get("drop_skill_invocation_messages", True)
        )
        self._ingest_max_content_chars = int(ingest_cfg.get("max_content_chars", 12000))
        self._skill_invoke_re = re.compile(
            r"\[SYSTEM:\s*The user has invoked the .* skill",
            re.IGNORECASE | re.DOTALL,
        )

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

        # Hindsight integration
        hindsight_cfg = config.get("hindsight", {})
        self.hindsight_enabled = bool(hindsight_cfg.get("enabled", False))
        self._hindsight_client: Optional[Any] = None # Will be initialized in .initialize()
        self._hindsight_retain_async = bool(hindsight_cfg.get("retain_async", True))
        self._hindsight_retain_every_n_turns = max(
            1,
            int(hindsight_cfg.get("retain_every_n_turns", 1) or 1),
        )
        self._hindsight_recall_method = str(hindsight_cfg.get("recall_method", "recall"))
        self._hindsight_recall_max_results = int(hindsight_cfg.get("recall_max_results", 4))
        self._hindsight_timeout = int(hindsight_cfg.get("timeout_ms", 8000)) / 1000
        self._hindsight_bank_id = str(hindsight_cfg.get("bank_id", "hermes")).strip() or "hermes"
        self._hindsight_budget = str(hindsight_cfg.get("recall_budget", "mid")).strip() or "mid"
        self._hindsight_mode = str(hindsight_cfg.get("mode", "local_external")).strip()
        self._hindsight_client_flavor = ""
        # End Hindsight integration

        ondemand_cfg = config.get("ondemand", {})
        self.ondemand_enabled = bool(ondemand_cfg.get("enabled", True))
        self._ondemand_default_recall = bool(ondemand_cfg.get("default_recall", False))
        self._ondemand_explicit_only = bool(ondemand_cfg.get("explicit_query_only", True))
        self._ondemand_max_results = int(ondemand_cfg.get("max_results", 3))
        self._ondemand_min_priority = int(ondemand_cfg.get("min_priority", 0))
        self._ondemand_keywords = self._normalize_keyword_list(
            ondemand_cfg.get("explicit_query_keywords", [])
        )
        if not self._ondemand_keywords:
            self._ondemand_keywords = [
                "回忆",
                "记得",
                "历史",
                "之前",
                "上次",
                "过去",
                "history",
                "remember",
                "recall",
                "context",
            ]

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hindsight_state (
                    bank_id TEXT PRIMARY KEY,
                    last_backfill_turn_id INTEGER NOT NULL DEFAULT 0,
                    live_turn_counter INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hindsight_retain_log (
                    retain_hash TEXT PRIMARY KEY,
                    bank_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            if self.fts_enabled:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_turns_fts
                    USING fts5(content, session_id UNINDEXED, role UNINDEXED)
                    """
                )
            if self.ondemand_enabled:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_ondemand_notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dedup_hash TEXT UNIQUE,
                        session_id TEXT,
                        note TEXT NOT NULL,
                        tags TEXT,
                        priority INTEGER NOT NULL DEFAULT 0,
                        source TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        nsfw_tag INTEGER NOT NULL DEFAULT 0,
                        nsfw_reason TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_ondemand_fts
                    USING fts5(note, tags UNINDEXED)
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

        # Hindsight initialization
        if self.hindsight_enabled:
            try:
                hindsight_cfg = self.config.get("hindsight", {})
                api_key = str(
                    hindsight_cfg.get("api_key") or os.environ.get("HINDSIGHT_API_KEY", "")
                ).strip()
                api_url = str(
                    hindsight_cfg.get("api_url") or os.environ.get("HINDSIGHT_API_URL", "")
                ).strip()
                if not api_url and self._hindsight_mode == "local_external":
                    api_url = "http://127.0.0.1:8882"
                if not api_url:
                    raise ValueError("hindsight api_url is empty")

                try:
                    # Older adapter API.
                    from hindsight_client import HindsightClient  # type: ignore

                    self._hindsight_client = HindsightClient(
                        api_key=api_key,
                        api_url=api_url,
                        bank_id=self._hindsight_bank_id,
                        budget=self._hindsight_budget,
                        mode=self._hindsight_mode,
                        timeout=self._hindsight_timeout,
                    )
                    self._hindsight_client_flavor = "legacy"
                except Exception:
                    # Current hindsight-client API.
                    from hindsight_client import Hindsight  # type: ignore

                    kwargs: Dict[str, Any] = {"base_url": api_url, "timeout": self._hindsight_timeout}
                    if api_key:
                        kwargs["api_key"] = api_key
                    self._hindsight_client = Hindsight(**kwargs)
                    self._hindsight_client_flavor = "modern"
                    self._ensure_hindsight_bank()

                logger.info(
                    "Hindsight client initialized (mode=%s, bank_id=%s, flavor=%s, api_url=%s)",
                    self._hindsight_mode,
                    self._hindsight_bank_id,
                    self._hindsight_client_flavor,
                    api_url,
                )
            except Exception as exc:
                logger.warning("Hindsight init failed (non-fatal): %s", exc)
                self._hindsight_client = None
        # End Hindsight initialization

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

        # Hindsight prefetch
        if self.hindsight_enabled and self._hindsight_client:
            try:
                h_result = self._hindsight_query(query)

                if h_result:
                    sections.append("# Hindsight Recall\n" + h_result)
            except Exception as exc:
                logger.warning("Hindsight prefetch failed (non-fatal): %s", exc)
        # End Hindsight prefetch

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

        if self.ondemand_enabled and self._should_recall_ondemand(query):
            try:
                ondemand_rows = self.search_ondemand(
                    query=query,
                    limit=self._ondemand_max_results,
                    min_priority=self._ondemand_min_priority,
                )
                if ondemand_rows:
                    lines = ["# On-demand Memory Notes"]
                    for row in ondemand_rows:
                        note = str(row.get("note", "")).replace("\n", " ").strip()
                        if len(note) > 220:
                            note = note[:220] + "..."
                        tags = ", ".join(row.get("tags", []) or [])
                        priority = int(row.get("priority", 0) or 0)
                        tag_part = f" [{tags}]" if tags else ""
                        lines.append(f"- [p{priority}]{tag_part} {note}")
                    sections.append("\n".join(lines))
            except Exception as exc:
                logger.warning("On-demand recall failed (non-fatal): %s", exc)

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

        # Hindsight retain
        if self.hindsight_enabled and self._hindsight_client:
            try:
                transcript = f"User: {user_message}\nAssistant: {assistant_message}"
                safe_metadata = self._normalize_hindsight_metadata(metadata or {})
                if not self._should_retain_live_turn():
                    return sync_stats
                source_key = self._build_live_source_key(transcript, safe_metadata)
                if self._hindsight_retain_async:
                    threading.Thread(
                        target=self._hindsight_retain_once,
                        args=(transcript, safe_metadata, source_key),
                        daemon=True,
                    ).start()
                else:
                    self._hindsight_retain_once(transcript, safe_metadata, source_key)
            except Exception as exc:
                logger.warning("Hindsight retain failed (non-fatal): %s", exc)
        # End Hindsight retain

        return sync_stats

    def hindsight_backfill(
        self,
        *,
        max_items: int = 500,
        from_turn_id: Optional[int] = None,
        session_id: str = "",
        include_nsfw: bool = True,
        dry_run: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        if not self.hindsight_enabled or not self._hindsight_client:
            return {"success": False, "error": "hindsight is disabled or unavailable"}

        max_items = max(1, min(int(max_items), 5000))
        checkpoint = 0 if force else self._get_hindsight_backfill_checkpoint()
        start_id = int(from_turn_id) if from_turn_id is not None else checkpoint

        sql = [
            "SELECT id, session_id, role, content, created_at, nsfw_tag",
            "FROM memory_turns",
            "WHERE id > ?",
        ]
        params: List[Any] = [start_id]
        if session_id:
            sql.append("AND session_id = ?")
            params.append(session_id)
        if not include_nsfw:
            sql.append("AND nsfw_tag = 0")
        sql.append("ORDER BY id ASC LIMIT ?")
        params.append(max_items)

        with self._connect_index() as conn:
            raw_rows = conn.execute("\n".join(sql), tuple(params)).fetchall()

        episodes = self._build_backfill_episodes(raw_rows)
        scanned_rows = len(raw_rows)
        scanned_episodes = len(episodes)
        retained = 0
        duplicates = 0
        failed = 0
        last_ok_turn_id = start_id
        last_seen_turn_id = start_id
        errors: List[str] = []

        for episode in episodes:
            source_key = str(episode["source_key"])
            transcript = str(episode["transcript"])
            metadata = dict(episode["metadata"])
            end_turn_id = int(episode["end_turn_id"])
            last_seen_turn_id = max(last_seen_turn_id, end_turn_id)
            if dry_run:
                retained += 1
                last_ok_turn_id = end_turn_id
                continue
            try:
                status = self._hindsight_retain_once(transcript, metadata, source_key)
                if status.get("retained"):
                    retained += 1
                    last_ok_turn_id = end_turn_id
                elif status.get("duplicate"):
                    duplicates += 1
                    last_ok_turn_id = end_turn_id
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 10:
                    errors.append(f"{source_key}: {exc}")

        if not dry_run and scanned_rows > 0:
            self._set_hindsight_backfill_checkpoint(last_ok_turn_id)

        return {
            "success": True,
            "dry_run": dry_run,
            "bank_id": self._hindsight_bank_id,
            "start_turn_id": start_id,
            "last_seen_turn_id": last_seen_turn_id,
            "last_checkpoint_turn_id": last_ok_turn_id if not dry_run else self._get_hindsight_backfill_checkpoint(),
            "scanned_rows": scanned_rows,
            "scanned_episodes": scanned_episodes,
            "retained": retained,
            "duplicates": duplicates,
            "failed": failed,
            "errors": errors,
        }

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
            skipped = 0
            for row in new_rows:
                if not self._should_ingest_row(row):
                    skipped += 1
                    continue
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
            "skipped": skipped,
            "last_source_message_id": new_rows[-1]["source_message_id"] if new_rows else last_id,
        }

    def add_ondemand_note(
        self,
        note: str,
        *,
        tags: Optional[List[str]] = None,
        priority: int = 1,
        session_id: str = "",
        source: str = "manual",
    ) -> Dict[str, Any]:
        if not self.ondemand_enabled:
            return {"success": False, "error": "ondemand memory is disabled in config"}
        clean_note = str(note or "").strip()
        if not clean_note:
            return {"success": False, "error": "note cannot be empty"}
        clean_tags = self._normalize_keyword_list(tags or [])
        priority = max(0, min(int(priority), 3))
        nsfw_tag, nsfw_reason = self._classify_nsfw(clean_note) if self.nsfw_save else (0, "")
        dedup_src = f"{clean_note.lower()}|{'|'.join(clean_tags)}|{priority}"
        dedup_hash = hashlib.sha256(dedup_src.encode("utf-8")).hexdigest()
        now = self._now_iso()
        tags_json = json.dumps(clean_tags, ensure_ascii=False)

        with self._connect_index() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO memory_ondemand_notes
                (dedup_hash, session_id, note, tags, priority, source, created_at, updated_at, nsfw_tag, nsfw_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedup_hash,
                    session_id,
                    clean_note,
                    tags_json,
                    priority,
                    source,
                    now,
                    now,
                    nsfw_tag,
                    nsfw_reason,
                ),
            )
            inserted = bool(cur.rowcount)
            if inserted and self.fts_enabled and (self.nsfw_index or nsfw_tag == 0):
                conn.execute(
                    """
                    INSERT INTO memory_ondemand_fts (rowid, note, tags)
                    VALUES (?, ?, ?)
                    """,
                    (cur.lastrowid, clean_note, " ".join(clean_tags)),
                )
            conn.commit()

        return {
            "success": True,
            "inserted": inserted,
            "message": "note stored" if inserted else "duplicate ignored",
            "note": clean_note,
            "tags": clean_tags,
            "priority": priority,
            "nsfw_tag": nsfw_tag,
        }

    def search_ondemand(
        self,
        *,
        query: str,
        limit: int = 5,
        min_priority: int = 0,
        include_nsfw: bool = True,
    ) -> List[Dict[str, Any]]:
        if not self.ondemand_enabled:
            return []
        limit = max(1, min(int(limit), 20))
        min_priority = max(0, min(int(min_priority), 3))
        q = str(query or "").strip()
        if not q:
            return []

        with self._connect_index() as conn:
            rows: List[Any] = []
            if self.fts_enabled:
                sql = (
                    """
                    SELECT n.id, n.session_id, n.note, n.tags, n.priority, n.source, n.created_at, n.nsfw_tag, n.nsfw_reason
                    FROM memory_ondemand_fts f
                    JOIN memory_ondemand_notes n ON n.id = f.rowid
                    WHERE f.note MATCH ? AND n.priority >= ?
                    """
                )
                params: List[Any] = [q, min_priority]
                if not include_nsfw:
                    sql += " AND n.nsfw_tag = 0"
                sql += " ORDER BY n.priority DESC, n.id DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()

            if not rows:
                sql = (
                    """
                    SELECT id, session_id, note, tags, priority, source, created_at, nsfw_tag, nsfw_reason
                    FROM memory_ondemand_notes
                    WHERE note LIKE ? AND priority >= ?
                    """
                )
                params = [f"%{q}%", min_priority]
                if not include_nsfw:
                    sql += " AND nsfw_tag = 0"
                sql += " ORDER BY priority DESC, id DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, tuple(params)).fetchall()

        out: List[Dict[str, Any]] = []
        for r in rows:
            parsed_tags: List[str] = []
            try:
                parsed = json.loads(r[3] or "[]")
                if isinstance(parsed, list):
                    parsed_tags = [str(x) for x in parsed if str(x).strip()]
            except Exception:
                parsed_tags = []
            out.append(
                {
                    "id": int(r[0]),
                    "session_id": str(r[1] or ""),
                    "note": str(r[2] or ""),
                    "tags": parsed_tags,
                    "priority": int(r[4] or 0),
                    "source": str(r[5] or ""),
                    "created_at": str(r[6] or ""),
                    "nsfw_tag": int(r[7] or 0),
                    "nsfw_reason": str(r[8] or ""),
                }
            )
        return out

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

    def _should_recall_ondemand(self, query: str) -> bool:
        if self._ondemand_default_recall:
            return True
        if not self._ondemand_explicit_only:
            return True
        q = str(query or "").lower()
        return any(tok in q for tok in self._ondemand_keywords)

    def _should_ingest_row(self, row: Dict[str, Any]) -> bool:
        role = str(row.get("role", "")).strip().lower()
        if self._ingest_allowed_roles and role not in self._ingest_allowed_roles:
            return False
        content = str(row.get("content", "") or "")
        if not content.strip():
            return False
        if self._ingest_max_content_chars > 0 and len(content) > self._ingest_max_content_chars:
            return False
        if self._ingest_drop_skill_invoke_banner and role == "user":
            if self._skill_invoke_re.search(content):
                return False
        return True

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

    def _ensure_hindsight_bank(self) -> None:
        if not self._hindsight_client:
            return
        create_fn = getattr(self._hindsight_client, "create_bank", None)
        if not callable(create_fn):
            return
        try:
            create_fn(bank_id=self._hindsight_bank_id, name=self._hindsight_bank_id)
        except Exception as exc:
            # Common case: already exists / conflict. Keep non-fatal.
            msg = str(exc).lower()
            if "already" in msg or "exists" in msg or "409" in msg or "conflict" in msg:
                return
            raise

    def _should_retain_live_turn(self) -> bool:
        n = max(1, int(self._hindsight_retain_every_n_turns or 1))
        with self._connect_index() as conn:
            row = conn.execute(
                """
                SELECT live_turn_counter
                FROM hindsight_state
                WHERE bank_id = ?
                """,
                (self._hindsight_bank_id,),
            ).fetchone()
            current = int(row[0]) if row and row[0] is not None else 0
            new_count = current + 1
            conn.execute(
                """
                INSERT INTO hindsight_state (bank_id, last_backfill_turn_id, live_turn_counter, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(bank_id) DO UPDATE SET
                    live_turn_counter = excluded.live_turn_counter,
                    updated_at = excluded.updated_at
                """,
                (self._hindsight_bank_id, new_count, self._now_iso()),
            )
            conn.commit()
        return (new_count % n) == 0

    def _build_live_source_key(self, transcript: str, metadata: Dict[str, str]) -> str:
        sid = str(metadata.get("session_id", "")).strip()
        payload = f"{sid}|{transcript}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        return f"live:{sid}:{digest}"

    def _get_hindsight_backfill_checkpoint(self) -> int:
        with self._connect_index() as conn:
            row = conn.execute(
                """
                SELECT last_backfill_turn_id
                FROM hindsight_state
                WHERE bank_id = ?
                """,
                (self._hindsight_bank_id,),
            ).fetchone()
        if not row or row[0] is None:
            return 0
        return max(0, int(row[0]))

    def _set_hindsight_backfill_checkpoint(self, turn_id: int) -> None:
        turn_id = max(0, int(turn_id))
        with self._connect_index() as conn:
            conn.execute(
                """
                INSERT INTO hindsight_state (bank_id, last_backfill_turn_id, live_turn_counter, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(bank_id) DO UPDATE SET
                    last_backfill_turn_id = CASE
                        WHEN excluded.last_backfill_turn_id > hindsight_state.last_backfill_turn_id
                        THEN excluded.last_backfill_turn_id
                        ELSE hindsight_state.last_backfill_turn_id
                    END,
                    updated_at = excluded.updated_at
                """,
                (self._hindsight_bank_id, turn_id, self._now_iso()),
            )
            conn.commit()

    def _build_backfill_episodes(self, raw_rows: List[Any]) -> List[Dict[str, Any]]:
        episodes: List[Dict[str, Any]] = []
        i = 0
        while i < len(raw_rows):
            row = raw_rows[i]
            row_id = int(row[0])
            sid = str(row[1] or "")
            role = str(row[2] or "").strip().lower()
            content = str(row[3] or "").strip()
            created_at = str(row[4] or "")
            if not content:
                i += 1
                continue

            if (
                role == "user"
                and i + 1 < len(raw_rows)
                and str(raw_rows[i + 1][2] or "").strip().lower() == "assistant"
                and str(raw_rows[i + 1][1] or "") == sid
            ):
                next_row = raw_rows[i + 1]
                aid = int(next_row[0])
                assistant_content = str(next_row[3] or "").strip()
                transcript = f"User: {content}\nAssistant: {assistant_content}"
                episodes.append(
                    {
                        "source_key": f"backfill:pair:{row_id}:{aid}",
                        "transcript": transcript,
                        "metadata": self._normalize_hindsight_metadata(
                            {
                                "session_id": sid,
                                "source": "local_memory_backfill",
                                "source_turn_ids": f"{row_id},{aid}",
                                "created_at": created_at,
                            }
                        ),
                        "end_turn_id": aid,
                    }
                )
                i += 2
                continue

            role_title = role.capitalize() if role else "Message"
            transcript = f"{role_title}: {content}"
            episodes.append(
                {
                    "source_key": f"backfill:turn:{row_id}",
                    "transcript": transcript,
                    "metadata": self._normalize_hindsight_metadata(
                        {
                            "session_id": sid,
                            "source": "local_memory_backfill",
                            "source_turn_ids": str(row_id),
                            "created_at": created_at,
                        }
                    ),
                    "end_turn_id": row_id,
                }
            )
            i += 1
        return episodes

    def _hindsight_query(self, query: str) -> str:
        if not self._hindsight_client:
            return ""

        max_tokens = max(64, self._hindsight_recall_max_results * 50)
        if self._hindsight_recall_method == "reflect":
            if self._hindsight_client_flavor == "legacy":
                reflect_response = self._hindsight_client.reflect(
                    query=query,
                    budget=self._hindsight_budget,
                    max_tokens=max_tokens,
                )
            else:
                reflect_response = self._hindsight_client.reflect(
                    bank_id=self._hindsight_bank_id,
                    query=query,
                    budget=self._hindsight_budget,
                    max_tokens=max_tokens,
                )
            if not reflect_response:
                return ""
            answer = getattr(reflect_response, "answer", None) or getattr(reflect_response, "text", None)
            return str(answer or "").strip()

        if self._hindsight_client_flavor == "legacy":
            recall_response = self._hindsight_client.recall(
                query=query,
                budget=self._hindsight_budget,
                max_tokens=max_tokens,
            )
        else:
            recall_response = self._hindsight_client.recall(
                bank_id=self._hindsight_bank_id,
                query=query,
                budget=self._hindsight_budget,
                max_tokens=max_tokens,
            )

        results = getattr(recall_response, "results", None) or []
        lines: List[str] = []
        for r in results:
            text = getattr(r, "content", None) or getattr(r, "text", None) or ""
            text = str(text).strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    def _hindsight_retain_once(
        self,
        transcript: str,
        metadata: Dict[str, str],
        source_key: str,
    ) -> Dict[str, Any]:
        if not self._hindsight_client:
            return {"retained": False, "duplicate": False}
        retain_hash = self._build_retain_hash(transcript, source_key)

        with self._connect_index() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO hindsight_retain_log
                (retain_hash, bank_id, source_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (retain_hash, self._hindsight_bank_id, source_key, self._now_iso()),
            )
            conn.commit()
            if not cur.rowcount:
                return {"retained": False, "duplicate": True}

        try:
            self._hindsight_retain(transcript, metadata)
        except Exception:
            # Roll back reservation so failed retains can be retried safely.
            with self._connect_index() as conn:
                conn.execute(
                    "DELETE FROM hindsight_retain_log WHERE retain_hash = ?",
                    (retain_hash,),
                )
                conn.commit()
            raise
        return {"retained": True, "duplicate": False}

    def _build_retain_hash(self, transcript: str, source_key: str) -> str:
        payload = f"{self._hindsight_bank_id}|{source_key}|{transcript}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _hindsight_retain(self, transcript: str, metadata: Dict[str, str]) -> None:
        if not self._hindsight_client:
            return
        kwargs = {
            "content": transcript,
            "context": "conversation between Hermes Agent and the User",
            "metadata": metadata,
        }
        if self._hindsight_client_flavor == "legacy":
            kwargs["bank_id"] = self._hindsight_bank_id
            self._hindsight_client.retain(**kwargs)
            return
        self._hindsight_client.retain(bank_id=self._hindsight_bank_id, **kwargs)

    @staticmethod
    def _normalize_hindsight_metadata(metadata: Dict[str, Any]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for k, v in (metadata or {}).items():
            key = str(k).strip()
            if not key:
                continue
            val = str(v).strip()
            if not val:
                continue
            out[key] = val[:500]
        return out
