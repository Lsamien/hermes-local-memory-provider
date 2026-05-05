from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote


class SchemaValidationError(RuntimeError):
    """Raised when configured SQLite schema mapping does not match actual DB."""


@dataclass(frozen=True)
class HermesSchema:
    table: str
    id_column: str
    session_column: str
    role_column: str
    content_column: str
    created_at_column: str


class HermesSQLiteCompatibilityAdapter:
    """Read-only adapter that normalizes Hermes SQLite rows into a stable schema."""

    def __init__(self, sqlite_path: str, schema_config: Dict[str, Any]):
        self.sqlite_path = Path(sqlite_path).expanduser().resolve()
        self.schema = HermesSchema(
            table=str(schema_config.get("table", "messages")),
            id_column=str(schema_config.get("id_column", "id")),
            session_column=str(schema_config.get("session_column", "session_id")),
            role_column=str(schema_config.get("role_column", "role")),
            content_column=str(schema_config.get("content_column", "content")),
            created_at_column=str(schema_config.get("created_at_column", "created_at")),
        )

    def validate_schema(self) -> Dict[str, Any]:
        if not self.sqlite_path.exists():
            raise SchemaValidationError(f"Hermes SQLite not found: {self.sqlite_path}")

        with self._connect_ro() as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (self.schema.table,),
            ).fetchone()
            if not table_exists:
                raise SchemaValidationError(
                    f"Configured table not found: {self.schema.table}"
                )

            columns = {
                str(row[1])
                for row in conn.execute(
                    f"PRAGMA table_info({self._quote_ident(self.schema.table)})"
                ).fetchall()
            }

        required = {
            self.schema.id_column,
            self.schema.session_column,
            self.schema.role_column,
            self.schema.content_column,
            self.schema.created_at_column,
        }
        missing = sorted(required - columns)
        if missing:
            raise SchemaValidationError(f"Missing configured columns: {missing}")

        return {
            "table": self.schema.table,
            "columns": sorted(columns),
            "required_columns": sorted(required),
        }

    def fetch_recent_messages(self, limit: int = 5) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        query = (
            f"SELECT {self._sel(self.schema.id_column)},"
            f" {self._sel(self.schema.session_column)},"
            f" {self._sel(self.schema.role_column)},"
            f" {self._sel(self.schema.content_column)},"
            f" {self._sel(self.schema.created_at_column)}"
            f" FROM {self._quote_ident(self.schema.table)}"
            f" ORDER BY {self._sel(self.schema.created_at_column)} DESC"
            " LIMIT ?"
        )
        with self._connect_ro() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [self._normalize_row(r) for r in rows]

    def fetch_new_messages(
        self,
        since_source_message_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        id_col = self._sel(self.schema.id_column)
        base = (
            f"SELECT {id_col},"
            f" {self._sel(self.schema.session_column)},"
            f" {self._sel(self.schema.role_column)},"
            f" {self._sel(self.schema.content_column)},"
            f" {self._sel(self.schema.created_at_column)}"
            f" FROM {self._quote_ident(self.schema.table)}"
        )

        params: List[Any] = []
        if since_source_message_id is not None and str(since_source_message_id).strip():
            base += f" WHERE {id_col} > ?"
            params.append(since_source_message_id)

        base += f" ORDER BY {id_col} ASC LIMIT ?"
        params.append(limit)

        with self._connect_ro() as conn:
            rows = conn.execute(base, tuple(params)).fetchall()
        return [self._normalize_row(r) for r in rows]

    def fetch_session_messages(self, session_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        query = (
            f"SELECT {self._sel(self.schema.id_column)},"
            f" {self._sel(self.schema.session_column)},"
            f" {self._sel(self.schema.role_column)},"
            f" {self._sel(self.schema.content_column)},"
            f" {self._sel(self.schema.created_at_column)}"
            f" FROM {self._quote_ident(self.schema.table)}"
            f" WHERE {self._sel(self.schema.session_column)} = ?"
            f" ORDER BY {self._sel(self.schema.id_column)} ASC"
            " LIMIT ?"
        )
        with self._connect_ro() as conn:
            rows = conn.execute(query, (session_id, limit)).fetchall()
        return [self._normalize_row(r) for r in rows]

    def _connect_ro(self) -> sqlite3.Connection:
        encoded = quote(str(self.sqlite_path), safe="/")
        uri = f"file:{encoded}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0)

    @staticmethod
    def _quote_ident(name: str) -> str:
        escaped = str(name).replace('"', '""')
        return f'"{escaped}"'

    def _sel(self, name: str) -> str:
        return self._quote_ident(name)

    def _normalize_row(self, row: Any) -> Dict[str, Any]:
        source_message_id = str(row[0]) if row[0] is not None else ""
        session_id = "" if row[1] is None else str(row[1])
        role = "" if row[2] is None else str(row[2])
        content = "" if row[3] is None else str(row[3])
        raw_created = row[4]

        return {
            "source_message_id": source_message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": self._normalize_timestamp(raw_created),
            "metadata": {
                "raw_created_at": raw_created,
                "source_table": self.schema.table,
            },
        }

    @staticmethod
    def _normalize_timestamp(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            try:
                dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                return dt.isoformat().replace("+00:00", "Z")
            except Exception:
                return str(value)
        return str(value)
