"""Local memory provider plugin.

Adapter-only provider layer:
- maps Hermes MemoryProvider lifecycle into the local orchestrator
- keeps business logic in orchestrator.py
- reads Hermes SQLite via compatibility_adapter.py in read-only mode
- fail-open by default so chat is never blocked by memory failures
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home

# Ensure ~/.hermes root is importable during plugin discovery/import phase.
_home_root = str(get_hermes_home())
if _home_root not in sys.path:
    sys.path.insert(0, _home_root)

from .orchestrator import MemoryOrchestrator
from .notes_kb import NotesKnowledgeBase

logger = logging.getLogger(__name__)


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out

LOCAL_MEMORY_ONDEMAND_WRITE_SCHEMA: Dict[str, Any] = {
    "name": "local_memory_ondemand_write",
    "description": (
        "Store a non-core memory note in local sidecar memory for on-demand recall. "
        "Use this for volatile/optional context that should not occupy MEMORY.md."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "Memory note content to store."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags to improve recall hit rate.",
            },
            "priority": {
                "type": "integer",
                "description": "Priority 0-3. Higher means more important during recall.",
                "minimum": 0,
                "maximum": 3,
            },
        },
        "required": ["note"],
    },
}

LOCAL_MEMORY_ONDEMAND_RECALL_SCHEMA: Dict[str, Any] = {
    "name": "local_memory_ondemand_recall",
    "description": (
        "Search notes from local on-demand memory. "
        "Use when the user asks for prior optional details or historical context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {
                "type": "integer",
                "description": "Max results (1-10).",
                "minimum": 1,
                "maximum": 10,
            },
            "min_priority": {
                "type": "integer",
                "description": "Only return notes with priority >= this value (0-3).",
                "minimum": 0,
                "maximum": 3,
            },
            "include_nsfw": {
                "type": "boolean",
                "description": "Whether to include NSFW-tagged notes.",
            },
        },
        "required": ["query"],
    },
}

LOCAL_NOTES_KB_IMPORT_SCHEMA: Dict[str, Any] = {
    "name": "local_notes_kb_import",
    "description": (
        "Import note files into an independent local notes knowledge base (sidecar SQLite). "
        "Supports files and directories; directory import can be recursive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute/relative file or directory paths to import.",
            },
            "recursive": {
                "type": "boolean",
                "description": "When importing directories, recurse into subdirectories.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags to attach to imported chunks.",
            },
            "chunk_chars": {
                "type": "integer",
                "minimum": 200,
                "maximum": 2000,
                "description": "Target chunk size in characters.",
            },
            "overlap_chars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 500,
                "description": "Chunk overlap in characters.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, only report what would be indexed.",
            },
        },
        "required": ["paths"],
    },
}

LOCAL_NOTES_KB_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "local_notes_kb_search",
    "description": "Search imported notes knowledge base and return relevant chunks.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum results.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Require returned chunks to include these tags.",
            },
            "path_contains": {
                "type": "string",
                "description": "Optional source path substring filter.",
            },
            "include_missing": {
                "type": "boolean",
                "description": "Include chunks whose source file is now missing (if previously imported).",
            },
        },
        "required": ["query"],
    },
}

LOCAL_NOTES_KB_SYNC_SCHEMA: Dict[str, Any] = {
    "name": "local_notes_kb_sync",
    "description": (
        "Incrementally sync note roots into notes knowledge base. "
        "Behavior for deleted source files follows configured policy (preserve/soft_delete/hard_delete)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "roots": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Root directories/files to sync.",
            },
            "recursive": {
                "type": "boolean",
                "description": "Recurse into root directories.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags to attach during sync.",
            },
            "chunk_chars": {
                "type": "integer",
                "minimum": 200,
                "maximum": 2000,
            },
            "overlap_chars": {
                "type": "integer",
                "minimum": 0,
                "maximum": 500,
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, no writes/deletes are applied.",
            },
        },
        "required": ["roots"],
    },
}

LOCAL_NOTES_KB_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "local_notes_kb_status",
    "description": "Show notes knowledge base status and statistics.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

LOCAL_MEMORY_HINDSIGHT_BACKFILL_SCHEMA: Dict[str, Any] = {
    "name": "local_memory_hindsight_backfill",
    "description": (
        "Backfill historical local_memory turns into Hindsight bank with checkpoint and de-dup."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "max_items": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5000,
                "description": "Maximum historical rows to scan in this run.",
            },
            "from_turn_id": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional explicit start turn id (exclusive).",
            },
            "session_id": {
                "type": "string",
                "description": "Optional session id filter.",
            },
            "include_nsfw": {
                "type": "boolean",
                "description": "Whether NSFW-tagged rows are included in backfill.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, only scan and estimate without writing to Hindsight.",
            },
            "force": {
                "type": "boolean",
                "description": "If true, ignore saved checkpoint and start from 0 unless from_turn_id is set.",
            },
        },
        "required": [],
    },
}


class LocalMemoryProvider(MemoryProvider):
    def __init__(self):
        self._session_id = ""
        self._hermes_home = get_hermes_home()
        self._fail_open = True
        self._orchestrator: Optional[MemoryOrchestrator] = None
        self._notes_kb: Optional[NotesKnowledgeBase] = None
        self._config: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "local_memory"

    def is_available(self) -> bool:
        cfg = self._load_config()
        sqlite_path = Path(str(cfg.get("hermes", {}).get("sqlite_path", ""))).expanduser()
        return sqlite_path.exists()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        if kwargs.get("hermes_home"):
            self._hermes_home = Path(str(kwargs["hermes_home"]))
        hermes_home_str = str(self._hermes_home)
        if hermes_home_str not in sys.path:
            sys.path.insert(0, hermes_home_str)

        self._config = self._load_config()
        self._fail_open = bool(
            self._config.get("provider_adapter", {}).get("fail_open", True)
        )

        orchestrator = MemoryOrchestrator(self._config)
        notes_kb = NotesKnowledgeBase(self._config)
        try:
            orchestrator.initialize()
            notes_kb.initialize()
        except Exception as exc:
            if self._fail_open:
                logger.warning("local_memory initialize failed (fail-open): %s", exc)
                self._orchestrator = None
                self._notes_kb = None
                return
            raise

        self._orchestrator = orchestrator
        self._notes_kb = notes_kb

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            LOCAL_MEMORY_ONDEMAND_WRITE_SCHEMA,
            LOCAL_MEMORY_ONDEMAND_RECALL_SCHEMA,
            LOCAL_NOTES_KB_IMPORT_SCHEMA,
            LOCAL_NOTES_KB_SEARCH_SCHEMA,
            LOCAL_NOTES_KB_SYNC_SCHEMA,
            LOCAL_NOTES_KB_STATUS_SCHEMA,
            LOCAL_MEMORY_HINDSIGHT_BACKFILL_SCHEMA,
        ]

    def system_prompt_block(self) -> str:
        if not self._orchestrator:
            return ""
        return (
            "# Local Memory Provider\n"
            "Local sidecar memory is enabled with fail-open mode. "
            "Core Hermes SQLite remains read-only for this provider.\n"
            "For non-core/optional memories, use local_memory_ondemand_write. "
            "When users ask for prior optional details, use local_memory_ondemand_recall.\n"
            "For software/programming documents and notes, use local_notes_kb_import / "
            "local_notes_kb_search against the independent notes KB. "
            "When historical memory must be imported into Hindsight, use local_memory_hindsight_backfill."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._orchestrator:
            return ""
        try:
            base = self._orchestrator.prefetch(
                query=query,
                context={"session_id": session_id or self._session_id},
            )
            notes_block = ""
            if self._notes_kb and str(query or "").strip():
                try:
                    hits = self._notes_kb.search(query=str(query), limit=3)
                    if hits:
                        lines = ["# Notes KB Recall"]
                        for item in hits:
                            src = str(item.get("source_path", ""))
                            idx = int(item.get("chunk_index", 0))
                            snippet = str(item.get("snippet", "")).replace("\n", " ").strip()
                            lines.append(f"- [{idx}] {src}: {snippet}")
                        notes_block = "\n".join(lines)
                except Exception as notes_exc:
                    logger.debug("notes_kb prefetch search failed: %s", notes_exc)
            if base and notes_block:
                return base + "\n\n" + notes_block
            return base or notes_block
        except Exception as exc:
            logger.warning("local_memory prefetch failed: %s", exc)
            if self._fail_open:
                return ""
            raise

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._orchestrator:
            return
        try:
            self._orchestrator.sync_turn(
                user_message=user_content,
                assistant_message=assistant_content,
                metadata={"session_id": session_id or self._session_id},
            )
        except Exception as exc:
            logger.warning("local_memory sync_turn failed: %s", exc)
            if not self._fail_open:
                raise

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._orchestrator:
            return
        try:
            self._orchestrator.on_session_end(session_id=self._session_id)
        except Exception as exc:
            logger.warning("local_memory on_session_end failed: %s", exc)
            if not self._fail_open:
                raise

    def shutdown(self) -> None:
        self._orchestrator = None
        self._notes_kb = None

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._orchestrator:
            return json.dumps(
                {"success": False, "error": "local_memory provider is not initialized"},
                ensure_ascii=False,
            )

        try:
            if tool_name == "local_memory_ondemand_write":
                result = self._orchestrator.add_ondemand_note(
                    note=str(args.get("note", "")),
                    tags=args.get("tags") if isinstance(args.get("tags"), list) else [],
                    priority=int(args.get("priority", 1)),
                    session_id=str(kwargs.get("session_id") or self._session_id or ""),
                    source="tool:local_memory_ondemand_write",
                )
                return json.dumps(result, ensure_ascii=False)

            if tool_name == "local_memory_ondemand_recall":
                items = self._orchestrator.search_ondemand(
                    query=str(args.get("query", "")),
                    limit=int(args.get("limit", 5)),
                    min_priority=int(args.get("min_priority", 0)),
                    include_nsfw=bool(args.get("include_nsfw", True)),
                )
                return json.dumps(
                    {
                        "success": True,
                        "count": len(items),
                        "items": items,
                    },
                    ensure_ascii=False,
                )

            if tool_name == "local_notes_kb_import":
                if not self._notes_kb:
                    return json.dumps({"success": False, "error": "notes_kb not initialized"}, ensure_ascii=False)
                paths = args.get("paths")
                if isinstance(paths, str):
                    paths = [paths]
                if not isinstance(paths, list):
                    return json.dumps({"success": False, "error": "paths must be an array of strings"}, ensure_ascii=False)
                result = self._notes_kb.import_paths(
                    paths=[str(p) for p in paths],
                    recursive=bool(args.get("recursive", True)),
                    tags=args.get("tags") if isinstance(args.get("tags"), list) else [],
                    chunk_chars=int(args.get("chunk_chars")) if args.get("chunk_chars") is not None else None,
                    overlap_chars=int(args.get("overlap_chars")) if args.get("overlap_chars") is not None else None,
                    dry_run=bool(args.get("dry_run", False)),
                )
                return json.dumps(result, ensure_ascii=False)

            if tool_name == "local_notes_kb_search":
                if not self._notes_kb:
                    return json.dumps({"success": False, "error": "notes_kb not initialized"}, ensure_ascii=False)
                items = self._notes_kb.search(
                    query=str(args.get("query", "")),
                    limit=int(args.get("limit")) if args.get("limit") is not None else None,
                    tags=args.get("tags") if isinstance(args.get("tags"), list) else [],
                    path_contains=str(args.get("path_contains", "")),
                    include_missing=bool(args.get("include_missing", True)),
                )
                return json.dumps(
                    {"success": True, "count": len(items), "items": items},
                    ensure_ascii=False,
                )

            if tool_name == "local_notes_kb_sync":
                if not self._notes_kb:
                    return json.dumps({"success": False, "error": "notes_kb not initialized"}, ensure_ascii=False)
                roots = args.get("roots")
                if isinstance(roots, str):
                    roots = [roots]
                if not isinstance(roots, list):
                    return json.dumps({"success": False, "error": "roots must be an array of strings"}, ensure_ascii=False)
                result = self._notes_kb.sync_roots(
                    roots=[str(p) for p in roots],
                    recursive=bool(args.get("recursive", True)),
                    tags=args.get("tags") if isinstance(args.get("tags"), list) else [],
                    chunk_chars=int(args.get("chunk_chars")) if args.get("chunk_chars") is not None else None,
                    overlap_chars=int(args.get("overlap_chars")) if args.get("overlap_chars") is not None else None,
                    dry_run=bool(args.get("dry_run", False)),
                )
                return json.dumps(result, ensure_ascii=False)

            if tool_name == "local_notes_kb_status":
                if not self._notes_kb:
                    return json.dumps({"success": False, "error": "notes_kb not initialized"}, ensure_ascii=False)
                return json.dumps({"success": True, **self._notes_kb.status()}, ensure_ascii=False)

            if tool_name == "local_memory_hindsight_backfill":
                result = self._orchestrator.hindsight_backfill(
                    max_items=int(args.get("max_items", 500)),
                    from_turn_id=int(args.get("from_turn_id")) if args.get("from_turn_id") is not None else None,
                    session_id=str(args.get("session_id", "")).strip(),
                    include_nsfw=bool(args.get("include_nsfw", True)),
                    dry_run=bool(args.get("dry_run", False)),
                    force=bool(args.get("force", False)),
                )
                return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning("local_memory tool call failed (%s): %s", tool_name, exc)
            if not self._fail_open:
                raise
            return json.dumps(
                {"success": False, "error": str(exc)},
                ensure_ascii=False,
            )

        return json.dumps(
            {"success": False, "error": f"unknown local_memory tool: {tool_name}"},
            ensure_ascii=False,
        )

    def _load_config(self) -> Dict[str, Any]:
        hermes_home = self._hermes_home
        root_config_path = hermes_home / "config.yaml"
        base_config_path = hermes_home / "plugins" / "local_memory" / "config.yaml"

        root_cfg = {}
        if root_config_path.exists():
            with root_config_path.open("r", encoding="utf-8") as f:
                root_cfg = yaml.safe_load(f) or {}

        custom_path = (
            root_cfg.get("memory", {})
            .get("local_memory", {})
            .get("config_path", "")
        )

        chosen = Path(custom_path).expanduser() if custom_path else base_config_path
        if not chosen.exists():
            raise FileNotFoundError(
                f"local_memory config not found: {chosen}"
            )

        with chosen.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        # Overlay memory-local overrides from root config so runtime values
        # (e.g. hindsight.bank_id/api_url) take effect without editing plugin defaults.
        memory_root_cfg = (root_cfg.get("memory", {}) or {})
        hindsight_override = memory_root_cfg.get("hindsight")
        if isinstance(hindsight_override, dict):
            cfg["hindsight"] = _deep_merge_dict(cfg.get("hindsight", {}) or {}, hindsight_override)

        return cfg


def register(ctx):
    ctx.register_memory_provider(LocalMemoryProvider())
