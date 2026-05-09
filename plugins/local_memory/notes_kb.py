from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    size: int
    mtime: float


class NotesKnowledgeBase:
    """Independent notes knowledge base (sidecar SQLite + FTS5).

    This store is intentionally separate from Hermes core `state.db`.
    """

    def __init__(self, config: Dict[str, Any]):
        cfg = config.get("notes_kb", {}) if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.sqlite_path = Path(
            str(cfg.get("sqlite_path", "memory/notes_kb/notes_kb.sqlite"))
        ).expanduser().resolve()
        self.default_chunk_chars = max(200, int(cfg.get("chunk_chars", 700)))
        self.default_overlap_chars = max(0, int(cfg.get("overlap_chars", 120)))
        self.default_search_limit = max(1, min(int(cfg.get("search_default_limit", 6)), 20))
        self.deletion_policy = str(cfg.get("source_deletion_policy", "preserve")).strip().lower()
        if self.deletion_policy not in {"preserve", "soft_delete", "hard_delete"}:
            self.deletion_policy = "preserve"
        self.include_extensions = {
            str(ext).lower() for ext in cfg.get("include_extensions", []) if str(ext).strip()
        } or {
            ".md", ".txt", ".rst", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml",
            ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh", ".sql",
            ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".swift", ".kt", ".rb",
            ".php", ".css", ".scss", ".html", ".xml",
        }

    def initialize(self) -> None:
        if not self.enabled:
            return
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes_sources (
                    source_path TEXT PRIMARY KEY,
                    file_size INTEGER NOT NULL,
                    file_mtime REAL NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    title TEXT,
                    tags TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    heading TEXT,
                    tags TEXT,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notes_chunks_source
                ON notes_chunks(source_path)
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_chunks_fts
                USING fts5(chunk_text, source_path UNINDEXED, heading UNINDEXED, tags UNINDEXED)
                """
            )
            conn.commit()

    def status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        with self._connect() as conn:
            src_count = int(conn.execute("SELECT COUNT(*) FROM notes_sources").fetchone()[0] or 0)
            chunk_count = int(conn.execute("SELECT COUNT(*) FROM notes_chunks").fetchone()[0] or 0)
            total_chars = int(conn.execute("SELECT COALESCE(SUM(length(chunk_text)), 0) FROM notes_chunks").fetchone()[0] or 0)
            active_count = int(conn.execute("SELECT COUNT(*) FROM notes_sources WHERE status='active'").fetchone()[0] or 0)
        return {
            "enabled": True,
            "sqlite_path": str(self.sqlite_path),
            "sources_total": src_count,
            "sources_active": active_count,
            "chunks_total": chunk_count,
            "total_chars": total_chars,
            "deletion_policy": self.deletion_policy,
        }

    def import_paths(
        self,
        paths: Iterable[str],
        *,
        recursive: bool = True,
        tags: Optional[List[str]] = None,
        chunk_chars: Optional[int] = None,
        overlap_chars: Optional[int] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"success": False, "error": "notes_kb is disabled"}
        source_files = self._collect_files(paths, recursive=recursive)
        if not source_files:
            return {"success": True, "scanned": 0, "indexed": 0, "updated": 0, "skipped": 0}

        cfg_chunk = max(200, int(chunk_chars or self.default_chunk_chars))
        cfg_overlap = max(0, int(overlap_chars if overlap_chars is not None else self.default_overlap_chars))
        clean_tags = self._normalize_tags(tags or [])

        indexed = 0
        updated = 0
        skipped = 0
        errors: List[str] = []

        for src in source_files:
            try:
                text = self._read_source_text(src.path)
                if not text.strip():
                    skipped += 1
                    continue
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                title = self._extract_title(src.path, text)
                exists, old_hash = self._get_source_hash(src.path)
                if exists and old_hash == content_hash:
                    skipped += 1
                    continue
                chunks = self._chunk_text(text, chunk_chars=cfg_chunk, overlap_chars=cfg_overlap)
                if dry_run:
                    if exists:
                        updated += 1
                    else:
                        indexed += 1
                    continue
                self._upsert_source_with_chunks(
                    src=src,
                    title=title,
                    tags=clean_tags,
                    content_hash=content_hash,
                    chunks=chunks,
                )
                if exists:
                    updated += 1
                else:
                    indexed += 1
            except Exception as exc:
                logger.warning("notes_kb import failed for %s: %s", src.path, exc)
                errors.append(f"{src.path}: {exc}")

        result: Dict[str, Any] = {
            "success": True,
            "scanned": len(source_files),
            "indexed": indexed,
            "updated": updated,
            "skipped": skipped,
            "dry_run": dry_run,
        }
        if errors:
            result["errors"] = errors[:20]
            result["error_count"] = len(errors)
        return result

    def sync_roots(
        self,
        roots: Iterable[str],
        *,
        recursive: bool = True,
        tags: Optional[List[str]] = None,
        chunk_chars: Optional[int] = None,
        overlap_chars: Optional[int] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        result = self.import_paths(
            roots,
            recursive=recursive,
            tags=tags,
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
            dry_run=dry_run,
        )
        if not self.enabled:
            return result

        roots_resolved = [str(Path(r).expanduser().resolve()) for r in roots if str(r).strip()]
        if not roots_resolved:
            return result

        missing_sources = self._find_missing_sources_under_roots(roots_resolved)
        result["missing_sources"] = len(missing_sources)
        result["deletion_policy"] = self.deletion_policy

        if dry_run or not missing_sources:
            return result

        if self.deletion_policy == "preserve":
            return result
        if self.deletion_policy == "soft_delete":
            with self._connect() as conn:
                now = self._now_iso()
                conn.executemany(
                    "UPDATE notes_sources SET status='missing', updated_at=? WHERE source_path=?",
                    [(now, p) for p in missing_sources],
                )
                conn.commit()
            result["soft_deleted"] = len(missing_sources)
            return result
        if self.deletion_policy == "hard_delete":
            removed = 0
            with self._connect() as conn:
                for path in missing_sources:
                    row_ids = [
                        int(r[0])
                        for r in conn.execute(
                            "SELECT id FROM notes_chunks WHERE source_path=?",
                            (path,),
                        ).fetchall()
                    ]
                    if row_ids:
                        conn.executemany(
                            "DELETE FROM notes_chunks_fts WHERE rowid=?",
                            [(rid,) for rid in row_ids],
                        )
                    conn.execute("DELETE FROM notes_chunks WHERE source_path=?", (path,))
                    conn.execute("DELETE FROM notes_sources WHERE source_path=?", (path,))
                    removed += 1
                conn.commit()
            result["hard_deleted"] = removed
        return result

    def search(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        tags: Optional[List[str]] = None,
        path_contains: str = "",
        include_missing: bool = True,
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        q = str(query or "").strip()
        if not q:
            return []
        lim = max(1, min(int(limit or self.default_search_limit), 20))
        tag_list = self._normalize_tags(tags or [])
        path_filter = str(path_contains or "").strip().lower()

        with self._connect() as conn:
            rows = []
            fts_sql = (
                """
                SELECT c.id, c.source_path, c.chunk_index, c.chunk_text, c.heading, c.tags, s.status
                FROM notes_chunks_fts f
                JOIN notes_chunks c ON c.id = f.rowid
                LEFT JOIN notes_sources s ON s.source_path = c.source_path
                WHERE f.chunk_text MATCH ?
                """
            )
            fts_params: List[Any] = [q]
            if path_filter:
                fts_sql += " AND lower(c.source_path) LIKE ?"
                fts_params.append(f"%{path_filter}%")
            if not include_missing:
                fts_sql += " AND COALESCE(s.status,'active')='active'"
            fts_sql += " ORDER BY c.id DESC LIMIT ?"
            fts_params.append(lim)
            try:
                rows = conn.execute(fts_sql, tuple(fts_params)).fetchall()
            except sqlite3.OperationalError:
                rows = []

            if not rows:
                like_sql = (
                    """
                    SELECT c.id, c.source_path, c.chunk_index, c.chunk_text, c.heading, c.tags, s.status
                    FROM notes_chunks c
                    LEFT JOIN notes_sources s ON s.source_path = c.source_path
                    WHERE c.chunk_text LIKE ?
                    """
                )
                like_params: List[Any] = [f"%{q}%"]
                if path_filter:
                    like_sql += " AND lower(c.source_path) LIKE ?"
                    like_params.append(f"%{path_filter}%")
                if not include_missing:
                    like_sql += " AND COALESCE(s.status,'active')='active'"
                like_sql += " ORDER BY c.id DESC LIMIT ?"
                like_params.append(lim)
                rows = conn.execute(like_sql, tuple(like_params)).fetchall()

        items: List[Dict[str, Any]] = []
        for r in rows:
            row_tags = self._normalize_tags(self._parse_json_array(r[5]))
            if tag_list and not set(tag_list).issubset(set(row_tags)):
                continue
            text = str(r[3] or "")
            items.append(
                {
                    "id": int(r[0]),
                    "source_path": str(r[1] or ""),
                    "chunk_index": int(r[2] or 0),
                    "heading": str(r[4] or ""),
                    "tags": row_tags,
                    "status": str(r[6] or "active"),
                    "snippet": (text[:360] + "...") if len(text) > 360 else text,
                }
            )
            if len(items) >= lim:
                break
        return items

    def _upsert_source_with_chunks(
        self,
        *,
        src: SourceFile,
        title: str,
        tags: List[str],
        content_hash: str,
        chunks: List[Tuple[str, str]],
    ) -> None:
        now = self._now_iso()
        source_path = str(src.path)
        tags_json = json.dumps(tags, ensure_ascii=False)
        with self._connect() as conn:
            old_ids = [
                int(r[0]) for r in conn.execute(
                    "SELECT id FROM notes_chunks WHERE source_path=?",
                    (source_path,),
                ).fetchall()
            ]
            if old_ids:
                conn.executemany("DELETE FROM notes_chunks_fts WHERE rowid=?", [(rid,) for rid in old_ids])
            conn.execute("DELETE FROM notes_chunks WHERE source_path=?", (source_path,))

            conn.execute(
                """
                INSERT INTO notes_sources
                (source_path, file_size, file_mtime, content_hash, status, title, tags, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    file_size=excluded.file_size,
                    file_mtime=excluded.file_mtime,
                    content_hash=excluded.content_hash,
                    status='active',
                    title=excluded.title,
                    tags=excluded.tags,
                    updated_at=excluded.updated_at
                """,
                (
                    source_path,
                    src.size,
                    src.mtime,
                    content_hash,
                    title,
                    tags_json,
                    now,
                ),
            )

            for idx, (heading, chunk_text) in enumerate(chunks):
                cur = conn.execute(
                    """
                    INSERT INTO notes_chunks
                    (source_path, chunk_index, chunk_text, heading, tags, content_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_path,
                        idx,
                        chunk_text,
                        heading,
                        tags_json,
                        content_hash,
                        now,
                        now,
                    ),
                )
                rowid = int(cur.lastrowid)
                conn.execute(
                    """
                    INSERT INTO notes_chunks_fts(rowid, chunk_text, source_path, heading, tags)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (rowid, chunk_text, source_path, heading, " ".join(tags)),
                )
            conn.commit()

    def _get_source_hash(self, path: Path) -> Tuple[bool, str]:
        source_path = str(path.resolve())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM notes_sources WHERE source_path=?",
                (source_path,),
            ).fetchone()
        if not row:
            return False, ""
        return True, str(row[0] or "")

    def _find_missing_sources_under_roots(self, roots: List[str]) -> List[str]:
        with self._connect() as conn:
            all_sources = [str(r[0]) for r in conn.execute("SELECT source_path FROM notes_sources").fetchall()]
        missing: List[str] = []
        for sp in all_sources:
            low = sp.lower()
            if not any(low.startswith(root.lower()) for root in roots):
                continue
            if not Path(sp).exists():
                missing.append(sp)
        return missing

    def _collect_files(self, paths: Iterable[str], *, recursive: bool) -> List[SourceFile]:
        files: List[SourceFile] = []
        seen = set()
        for raw in paths:
            p = Path(str(raw or "").strip()).expanduser()
            if not str(p).strip():
                continue
            if not p.exists():
                continue
            if p.is_file():
                if self._allow_file(p):
                    rp = p.resolve()
                    if str(rp) not in seen:
                        seen.add(str(rp))
                        st = rp.stat()
                        files.append(SourceFile(path=rp, size=int(st.st_size), mtime=float(st.st_mtime)))
                continue
            if p.is_dir():
                iterator = p.rglob("*") if recursive else p.glob("*")
                for child in iterator:
                    if not child.is_file():
                        continue
                    if not self._allow_file(child):
                        continue
                    rp = child.resolve()
                    if str(rp) in seen:
                        continue
                    seen.add(str(rp))
                    st = rp.stat()
                    files.append(SourceFile(path=rp, size=int(st.st_size), mtime=float(st.st_mtime)))
        return files

    def _allow_file(self, path: Path) -> bool:
        ext = path.suffix.lower()
        return ext in self.include_extensions or ext in {".pdf", ".docx"}

    def _read_source_text(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".pdf":
            return self._read_pdf(path)
        if ext == ".docx":
            return self._read_docx(path)
        return self._read_text_file(path)

    @staticmethod
    def _read_text_file(path: Path) -> str:
        encodings = ("utf-8", "utf-16", "gb18030", "latin-1")
        for enc in encodings:
            try:
                return path.read_text(encoding=enc)
            except Exception:
                continue
        return path.read_text(encoding="utf-8", errors="ignore")

    @staticmethod
    def _read_pdf(path: Path) -> str:
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            parts: List[str] = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(text)
            return "\n\n".join(parts)
        except Exception as exc:
            logger.warning("PDF parse failed for %s: %s", path, exc)
            return ""

    @staticmethod
    def _read_docx(path: Path) -> str:
        try:
            import docx  # type: ignore

            doc = docx.Document(str(path))
            lines = [p.text for p in doc.paragraphs if str(p.text).strip()]
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("DOCX parse failed for %s: %s", path, exc)
            return ""

    @staticmethod
    def _extract_title(path: Path, text: str) -> str:
        for line in text.splitlines():
            s = line.strip().strip("#").strip()
            if s:
                return s[:200]
        return path.stem

    @staticmethod
    def _chunk_text(text: str, *, chunk_chars: int, overlap_chars: int) -> List[Tuple[str, str]]:
        lines = [ln.rstrip() for ln in text.splitlines()]
        paras: List[str] = []
        heading = ""
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("#"):
                heading = s.lstrip("#").strip()[:160]
                continue
            if s:
                paras.append((heading + "\n" + s).strip() if heading else s)

        if not paras:
            plain = text.strip()
            return [("", plain[:chunk_chars])] if plain else []

        chunks: List[Tuple[str, str]] = []
        cur = ""
        cur_heading = ""
        for para in paras:
            p_heading = ""
            p_text = para
            if "\n" in para:
                p_heading, p_text = para.split("\n", 1)
            add = p_text.strip()
            if not add:
                continue
            candidate = (cur + "\n\n" + add).strip() if cur else add
            if len(candidate) <= chunk_chars:
                cur = candidate
                if not cur_heading:
                    cur_heading = p_heading
                continue
            if cur:
                chunks.append((cur_heading, cur))
                tail = cur[-overlap_chars:] if overlap_chars > 0 else ""
                cur = (tail + "\n\n" + add).strip() if tail else add
                cur_heading = p_heading or cur_heading
            else:
                # Single paragraph exceeds chunk size: hard split
                start = 0
                while start < len(add):
                    part = add[start:start + chunk_chars]
                    chunks.append((p_heading, part))
                    start += max(1, chunk_chars - overlap_chars)
                cur = ""
                cur_heading = ""
        if cur:
            chunks.append((cur_heading, cur))
        return chunks

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.sqlite_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_tags(items: Iterable[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for item in items:
            tag = str(item or "").strip().lower()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            out.append(tag)
        return out

    @staticmethod
    def _parse_json_array(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x) for x in value if str(x).strip()]
        try:
            parsed = json.loads(str(value))
            if isinstance(parsed, list):
                return [str(x) for x in parsed if str(x).strip()]
        except Exception:
            pass
        return []
