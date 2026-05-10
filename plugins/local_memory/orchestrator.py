from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compatibility_adapter import HermesSQLiteCompatibilityAdapter

logger = logging.getLogger(__name__)


class _Mem0HTTPClient:
    def __init__(self, api_url: str, api_key: str, timeout_s: float):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_s = max(1.0, float(timeout_s))

    def _request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.api_url}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read().decode("utf-8")
        if not raw.strip():
            return {}
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
            return {"raw": obj}
        except Exception:
            return {"raw": raw}

    def ping(self) -> tuple[bool, str]:
        try:
            self._request_json("GET", "/configure")
            return True, "ok"
        except urllib.error.HTTPError as exc:
            return False, f"http_{exc.code}"
        except Exception as exc:
            return False, str(exc)

    def add_memory(
        self,
        *,
        messages: List[Dict[str, str]],
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "messages": messages,
            "user_id": user_id,
        }
        if metadata:
            payload["metadata"] = metadata
        return self._request_json("POST", "/memories", payload)

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "query": query,
            "filters": {"user_id": user_id},
            "top_k": max(1, min(int(top_k), 20)),
        }
        return self._request_json("POST", "/search", payload)


class _Mem0LocalClient:
    """Best-effort Mem0 OSS library adapter.

    This keeps runtime resilient when Mem0 HTTP API is unavailable.
    """

    def __init__(self, local_cfg: Dict[str, Any], timeout_s: float):
        self.local_cfg = local_cfg
        self.timeout_s = max(1.0, float(timeout_s))
        self._memory: Optional[Any] = None
        self._init_error = ""

    def _build_config_dict(self) -> Dict[str, Any]:
        custom = self.local_cfg.get("config_dict")
        if isinstance(custom, dict) and custom:
            return custom

        cfg: Dict[str, Any] = {}
        llm_provider = str(
            self.local_cfg.get("llm_provider")
            or self.local_cfg.get("model_provider")
            or ""
        ).strip()
        llm_provider_alias = {
            "openai_compatible": "openai",
            "openai-compatible": "openai",
        }
        llm_provider = llm_provider_alias.get(llm_provider.lower(), llm_provider)
        llm_model = str(self.local_cfg.get("llm_model") or "").strip()
        llm_base_url = str(self.local_cfg.get("llm_base_url") or "").strip()
        llm_api_key = str(self.local_cfg.get("llm_api_key") or "").strip()
        if llm_provider.lower() == "openai" and not llm_api_key:
            # OpenAI-compatible gateways still require a non-empty key field.
            llm_api_key = "local-placeholder"

        if llm_provider:
            llm_cfg: Dict[str, Any] = {"provider": llm_provider}
            llm_inner: Dict[str, Any] = {}
            if llm_model:
                llm_inner["model"] = llm_model
            if llm_base_url:
                provider_key = llm_provider.lower()
                if provider_key == "openai":
                    llm_inner["openai_base_url"] = llm_base_url
                elif provider_key in {"xai", "x.ai"}:
                    # mem0's xai config does not currently expose xai_base_url on BaseLlmConfig.
                    # We pass this via XAI_API_BASE env var in _ensure_memory().
                    pass
                elif provider_key == "ollama":
                    llm_inner["ollama_base_url"] = llm_base_url
                else:
                    llm_inner["base_url"] = llm_base_url
            if llm_api_key:
                llm_inner["api_key"] = llm_api_key
            if llm_inner:
                llm_cfg["config"] = llm_inner
            cfg["llm"] = llm_cfg

        embedder_provider = str(
            self.local_cfg.get("embedder_provider") or "huggingface"
        ).strip()
        embedder_model = str(self.local_cfg.get("embedder_model") or "").strip()
        embedder_api_key = str(self.local_cfg.get("embedder_api_key") or "").strip()
        embedder_base_url = str(self.local_cfg.get("embedder_base_url") or "").strip()
        if embedder_model:
            emb_cfg: Dict[str, Any] = {"provider": embedder_provider}
            emb_inner: Dict[str, Any] = {"model": embedder_model}
            if embedder_api_key:
                emb_inner["api_key"] = embedder_api_key
            if embedder_base_url:
                e_provider = embedder_provider.lower()
                if e_provider == "openai":
                    emb_inner["openai_base_url"] = embedder_base_url
                elif e_provider == "huggingface":
                    emb_inner["huggingface_base_url"] = embedder_base_url
                elif e_provider == "ollama":
                    emb_inner["ollama_base_url"] = embedder_base_url
                else:
                    emb_inner["base_url"] = embedder_base_url
            emb_cfg["config"] = emb_inner
            cfg["embedder"] = emb_cfg

        vector_provider = str(
            self.local_cfg.get("vector_store_provider") or "qdrant"
        ).strip().lower()
        vector_path = str(self.local_cfg.get("storage_path") or "").strip()
        vector_collection = str(
            self.local_cfg.get("collection_name") or "mem0_local"
        ).strip()
        vector_dims = int(self.local_cfg.get("embedding_dims", 0) or 0)
        if vector_provider:
            vector_cfg: Dict[str, Any] = {"provider": vector_provider, "config": {}}
            if vector_provider == "qdrant":
                if vector_path:
                    vector_cfg["config"]["path"] = vector_path
                if vector_collection:
                    vector_cfg["config"]["collection_name"] = vector_collection
                if vector_dims > 0:
                    vector_cfg["config"]["embedding_model_dims"] = vector_dims
            if vector_cfg["config"]:
                cfg["vector_store"] = vector_cfg

        return cfg

    @staticmethod
    def _call_with_variants(fn: Any, variants: List[Dict[str, Any]]) -> Any:
        last_exc: Optional[Exception] = None
        for kwargs in variants:
            try:
                return fn(**kwargs)
            except TypeError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return fn()

    def _ensure_memory(self) -> Any:
        if self._memory is not None:
            return self._memory

        from mem0 import Memory  # type: ignore

        llm_provider = str(
            self.local_cfg.get("llm_provider")
            or self.local_cfg.get("model_provider")
            or ""
        ).strip().lower()
        llm_base_url = str(self.local_cfg.get("llm_base_url") or "").strip()
        llm_api_key = str(self.local_cfg.get("llm_api_key") or "").strip()
        if llm_provider in {"xai", "x.ai"}:
            if llm_base_url:
                os.environ.setdefault("XAI_API_BASE", llm_base_url)
            if llm_api_key:
                os.environ.setdefault("XAI_API_KEY", llm_api_key)
        elif llm_provider == "openai":
            if llm_base_url:
                os.environ.setdefault("OPENAI_BASE_URL", llm_base_url)
            if llm_api_key:
                os.environ.setdefault("OPENAI_API_KEY", llm_api_key)

        config_dict = self._build_config_dict()
        variants = []
        if config_dict:
            variants.extend(
                [
                    {"config_dict": config_dict},
                    {"config": config_dict},
                ]
            )
        variants.append({})

        last_exc: Optional[Exception] = None
        if hasattr(Memory, "from_config"):
            for kwargs in variants:
                try:
                    self._memory = Memory.from_config(**kwargs)
                    return self._memory
                except Exception as exc:
                    last_exc = exc
        for kwargs in variants:
            try:
                if kwargs:
                    self._memory = Memory(**kwargs)
                else:
                    self._memory = Memory()
                return self._memory
            except Exception as exc:
                last_exc = exc

        msg = str(last_exc) if last_exc else "mem0 local init failed"
        self._init_error = msg
        raise RuntimeError(msg)

    def ping(self) -> tuple[bool, str]:
        try:
            self._ensure_memory()
            return True, "ok"
        except Exception as exc:
            self._init_error = str(exc)
            return False, self._init_error or "init_failed"

    def add_memory(
        self,
        *,
        messages: List[Dict[str, str]],
        user_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        memory = self._ensure_memory()
        add_fn = getattr(memory, "add", None)
        if not callable(add_fn):
            raise RuntimeError("mem0 local client has no add()")

        text_payload = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}".strip() for m in messages
        ).strip()

        def _variants(infer_flag: Optional[bool]) -> List[Dict[str, Any]]:
            base = [
                {"messages": messages, "user_id": user_id, "metadata": metadata or {}},
                {"messages": messages, "user_id": user_id},
                {"messages": messages, "filters": {"user_id": user_id}, "metadata": metadata or {}},
                {"messages": messages, "filters": {"user_id": user_id}},
                {"text": text_payload, "user_id": user_id, "metadata": metadata or {}},
                {"text": text_payload, "user_id": user_id},
                {"text": text_payload, "filters": {"user_id": user_id}, "metadata": metadata or {}},
                {"text": text_payload, "filters": {"user_id": user_id}},
            ]
            if infer_flag is None:
                return base
            out: List[Dict[str, Any]] = []
            for item in base:
                tmp = dict(item)
                tmp["infer"] = infer_flag
                out.append(tmp)
            return out

        result: Any = None
        try:
            result = self._call_with_variants(add_fn, _variants(True))
        except Exception:
            result = self._call_with_variants(add_fn, _variants(False))
        else:
            if isinstance(result, dict):
                rows = result.get("results", None)
                if isinstance(rows, list) and not rows:
                    result = self._call_with_variants(add_fn, _variants(False))

        if isinstance(result, dict):
            return result
        return {"raw": result}

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int,
    ) -> Dict[str, Any]:
        memory = self._ensure_memory()
        search_fn = getattr(memory, "search", None)
        if not callable(search_fn):
            raise RuntimeError("mem0 local client has no search()")

        k = max(1, min(int(top_k), 20))
        variants = [
            {"query": query, "filters": {"user_id": user_id}, "top_k": k},
            {"query": query, "filters": {"user_id": user_id}, "limit": k},
            {"query": query, "user_id": user_id, "top_k": k},
            {"query": query, "user_id": user_id, "limit": k},
        ]
        result = self._call_with_variants(search_fn, variants)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"results": result}
        return {"raw": result}


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
        self._hindsight_api_url = ""
        self._hindsight_api_key = ""
        self._hindsight_client_thread_id: Optional[int] = None
        self._hindsight_retain_server_async = bool(hindsight_cfg.get("retain_server_async", True))
        self._hindsight_backfill_max_chars = max(
            500,
            int(hindsight_cfg.get("backfill_max_content_chars", 2500) or 2500),
        )
        self._hindsight_live_max_chars = max(
            500,
            int(hindsight_cfg.get("live_max_content_chars", 4000) or 4000),
        )
        # End Hindsight integration

        mem0_cfg = config.get("mem0", {})
        self.mem0_enabled = bool(mem0_cfg.get("enabled", False))
        self._mem0_client: Optional[Any] = None
        self._mem0_api_url = str(
            mem0_cfg.get("api_url") or os.environ.get("MEM0_API_URL", "")
        ).strip() or "http://127.0.0.1:18888"
        self._mem0_api_key = str(
            mem0_cfg.get("api_key") or os.environ.get("MEM0_API_KEY", "")
        ).strip()
        self._mem0_timeout_s = max(1.0, int(mem0_cfg.get("timeout_ms", 6000)) / 1000)
        self._mem0_recall_max_results = int(mem0_cfg.get("recall_max_results", 3))
        self._mem0_require_explicit_query = bool(mem0_cfg.get("explicit_query_only", True))
        self._mem0_retain_async = bool(mem0_cfg.get("retain_async", True))
        self._mem0_live_max_chars = max(
            300,
            int(mem0_cfg.get("live_max_content_chars", 3000) or 3000),
        )
        self._mem0_fallback_mode = str(
            mem0_cfg.get("fallback_mode", "http_only")
        ).strip().lower()
        if self._mem0_fallback_mode not in {
            "http_only",
            "local_only",
            "http_then_local",
            "local_then_http",
        }:
            self._mem0_fallback_mode = "http_only"
        local_cfg = mem0_cfg.get("local_backend", {})
        if not isinstance(local_cfg, dict):
            local_cfg = {}
        self._mem0_local_cfg = local_cfg
        self._mem0_local_enabled = bool(local_cfg.get("enabled", False))
        self._mem0_local_client: Optional[_Mem0LocalClient] = None
        self._mem0_active_backend = "none"
        self._mem0_user_id = str(mem0_cfg.get("user_id", "")).strip()
        self._mem0_query_keywords = self._normalize_keyword_list(
            mem0_cfg.get("explicit_query_keywords", [])
        )
        if not self._mem0_query_keywords:
            self._mem0_query_keywords = [
                "回忆",
                "记得",
                "历史",
                "上次",
                "之前",
                "remember",
                "recall",
                "history",
                "memory",
            ]

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
        self._nsfw_keyword_rules = self._compile_nsfw_keyword_rules(self._nsfw_keywords)

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mem0_retain_log (
                    retain_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mem0_state (
                    user_id TEXT PRIMARY KEY,
                    last_backfill_turn_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
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
                self._hindsight_api_url = api_url
                self._hindsight_api_key = api_key

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
                    self._hindsight_client_thread_id = threading.get_ident()
                    # Modern hindsight-client internally uses aiohttp sessions bound
                    # to an event loop. Background thread writes can cross loop/thread
                    # boundaries and raise:
                    # "Timeout context manager should be used inside a task".
                    # Force synchronous retain for modern client to keep loop affinity stable.
                    if self._hindsight_retain_async:
                        logger.info(
                            "Hindsight modern client detected; forcing retain_async=false "
                            "to avoid cross-thread event-loop/session issues."
                        )
                        self._hindsight_retain_async = False
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

        if self.mem0_enabled:
            try:
                graphiti_group = str(
                    (self.config.get("graphiti", {}) or {}).get("group_id", "")
                ).strip()
                if not self._mem0_user_id:
                    self._mem0_user_id = (
                        str(self._hindsight_bank_id or "").strip()
                        or graphiti_group
                        or "default"
                    )
                self._init_mem0_backend()
            except Exception as exc:
                logger.warning("Mem0 init failed (non-fatal): %s", exc)
                self._mem0_client = None

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

        if self.mem0_enabled and self._mem0_client and self._should_recall_mem0(query):
            try:
                mem0_items = self._mem0_query(query)
                if mem0_items:
                    sections.append(
                        "# Mem0 Recall\n" + "\n".join(f"- {item}" for item in mem0_items)
                    )
            except Exception as exc:
                logger.warning("Mem0 prefetch failed (non-fatal): %s", exc)

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
                safe_user = self._clip_hindsight_content(str(user_message or ""), self._hindsight_live_max_chars)
                safe_assistant = self._clip_hindsight_content(
                    str(assistant_message or ""),
                    self._hindsight_live_max_chars,
                )
                transcript = f"User: {safe_user}\nAssistant: {safe_assistant}"
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

        if self.mem0_enabled and self._mem0_client:
            try:
                safe_user = self._clip_mem0_content(str(user_message or ""))
                safe_assistant = self._clip_mem0_content(str(assistant_message or ""))
                if safe_user or safe_assistant:
                    transcript = f"User: {safe_user}\nAssistant: {safe_assistant}".strip()
                    nsfw_tag, nsfw_reason = self._classify_nsfw(transcript) if self.nsfw_save else (0, "")
                    mem0_metadata: Dict[str, Any] = {
                        "session_id": session_id or "unknown",
                        "source": "local_memory_sync_turn",
                    }
                    if nsfw_tag:
                        mem0_metadata["nsfw_tag"] = "1"
                        mem0_metadata["nsfw_reason"] = nsfw_reason or "keyword"
                    source_key = self._build_mem0_source_key(
                        session_id=session_id,
                        transcript=transcript,
                    )
                    if self._mem0_retain_async:
                        threading.Thread(
                            target=self._mem0_retain_once,
                            args=(safe_user, safe_assistant, mem0_metadata, source_key),
                            daemon=True,
                        ).start()
                    else:
                        self._mem0_retain_once(safe_user, safe_assistant, mem0_metadata, source_key)
            except Exception as exc:
                logger.warning("Mem0 retain failed (non-fatal): %s", exc)

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
        if self._ingest_allowed_roles:
            role_placeholders = ",".join("?" for _ in self._ingest_allowed_roles)
            sql.append(f"AND lower(role) IN ({role_placeholders})")
            params.extend(sorted(self._ingest_allowed_roles))
        if session_id:
            sql.append("AND session_id = ?")
            params.append(session_id)
        if not include_nsfw:
            sql.append("AND nsfw_tag = 0")
        sql.append("ORDER BY id ASC LIMIT ?")
        params.append(max_items)

        with self._connect_index() as conn:
            raw_rows = conn.execute("\n".join(sql), tuple(params)).fetchall()

        episodes = self._build_backfill_episodes(raw_rows, max_chars=self._hindsight_backfill_max_chars)
        scanned_rows = len(raw_rows)
        scanned_episodes = len(episodes)
        retained = 0
        duplicates = 0
        failed = 0
        last_ok_turn_id = start_id
        failure_seen = False
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
                    if not failure_seen:
                        last_ok_turn_id = end_turn_id
                elif status.get("duplicate"):
                    duplicates += 1
                    if not failure_seen:
                        last_ok_turn_id = end_turn_id
                else:
                    failed += 1
                    failure_seen = True
            except Exception as exc:
                failed += 1
                failure_seen = True
                if len(errors) < 10:
                    errors.append(f"{source_key}: {exc.__class__.__name__}: {exc}")

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

    def mem0_backfill(
        self,
        *,
        max_items: int = 500,
        from_turn_id: Optional[int] = None,
        session_id: str = "",
        include_nsfw: bool = True,
        dry_run: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        if not self.mem0_enabled or not self._mem0_client:
            return {"success": False, "error": "mem0 is disabled or unavailable"}

        max_items = max(1, min(int(max_items), 5000))
        checkpoint = 0 if force else self._get_mem0_backfill_checkpoint()
        start_id = int(from_turn_id) if from_turn_id is not None else checkpoint

        sql = [
            "SELECT id, session_id, role, content, created_at, nsfw_tag",
            "FROM memory_turns",
            "WHERE id > ?",
        ]
        params: List[Any] = [start_id]
        if self._ingest_allowed_roles:
            role_placeholders = ",".join("?" for _ in self._ingest_allowed_roles)
            sql.append(f"AND lower(role) IN ({role_placeholders})")
            params.extend(sorted(self._ingest_allowed_roles))
        if session_id:
            sql.append("AND session_id = ?")
            params.append(session_id)
        if not include_nsfw:
            sql.append("AND nsfw_tag = 0")
        sql.append("ORDER BY id ASC LIMIT ?")
        params.append(max_items)

        with self._connect_index() as conn:
            raw_rows = conn.execute("\n".join(sql), tuple(params)).fetchall()

        items = self._build_mem0_backfill_items(raw_rows, max_chars=self._mem0_live_max_chars)
        scanned_rows = len(raw_rows)
        scanned_items = len(items)
        retained = 0
        duplicates = 0
        failed = 0
        last_ok_turn_id = start_id
        failure_seen = False
        last_seen_turn_id = start_id
        errors: List[str] = []

        for item in items:
            source_key = str(item["source_key"])
            safe_user = str(item.get("safe_user", ""))
            safe_assistant = str(item.get("safe_assistant", ""))
            metadata = dict(item.get("metadata", {}))
            end_turn_id = int(item.get("end_turn_id", start_id))
            last_seen_turn_id = max(last_seen_turn_id, end_turn_id)

            if dry_run:
                retained += 1
                last_ok_turn_id = end_turn_id
                continue

            try:
                status = self._mem0_retain_once(
                    safe_user=safe_user,
                    safe_assistant=safe_assistant,
                    metadata=metadata,
                    source_key=source_key,
                )
                if status.get("retained"):
                    retained += 1
                    if not failure_seen:
                        last_ok_turn_id = end_turn_id
                elif status.get("duplicate"):
                    duplicates += 1
                    if not failure_seen:
                        last_ok_turn_id = end_turn_id
                else:
                    failed += 1
                    failure_seen = True
            except Exception as exc:
                failed += 1
                failure_seen = True
                if len(errors) < 10:
                    errors.append(f"{source_key}: {exc.__class__.__name__}: {exc}")

        if not dry_run and scanned_rows > 0:
            self._set_mem0_backfill_checkpoint(last_ok_turn_id)

        return {
            "success": True,
            "dry_run": dry_run,
            "user_id": self._mem0_user_id or "default",
            "start_turn_id": start_id,
            "last_seen_turn_id": last_seen_turn_id,
            "last_checkpoint_turn_id": (
                last_ok_turn_id if not dry_run else self._get_mem0_backfill_checkpoint()
            ),
            "scanned_rows": scanned_rows,
            "scanned_items": scanned_items,
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
        rows = conn.execute(
            """
            SELECT id, content
            FROM memory_turns
            WHERE nsfw_tag = 0
            """
        ).fetchall()
        for row in rows:
            row_id = int(row[0])
            tag, reason = self._classify_nsfw(row[1] or "")
            if tag:
                conn.execute(
                    """
                    UPDATE memory_turns
                    SET nsfw_tag = 1, nsfw_reason = COALESCE(NULLIF(nsfw_reason, ''), ?)
                    WHERE id = ?
                    """,
                    (reason, row_id),
                )

    def _classify_nsfw(self, content: Any) -> tuple[int, str]:
        text = str(content or "")
        if not text.strip():
            return 0, ""
        lowered = text.lower()
        for kw, pattern in self._nsfw_keyword_rules:
            if pattern is None:
                if kw in lowered:
                    return 1, f"keyword:{kw}"
            elif pattern.search(lowered):
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

    def _should_recall_mem0(self, query: str) -> bool:
        if not self._mem0_require_explicit_query:
            return True
        q = str(query or "").lower()
        return any(tok in q for tok in self._mem0_query_keywords)

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

    @staticmethod
    def _compile_nsfw_keyword_rules(keywords: List[str]) -> List[tuple[str, Optional[re.Pattern[str]]]]:
        rules: List[tuple[str, Optional[re.Pattern[str]]]] = []
        for kw in keywords:
            token = str(kw or "").strip().lower()
            if not token:
                continue
            if re.fullmatch(r"[a-z0-9+._-]+", token):
                pat = re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", re.IGNORECASE)
                rules.append((token, pat))
            else:
                rules.append((token, None))
        return rules

    def _init_mem0_backend(self) -> None:
        self._mem0_client = None
        self._mem0_active_backend = "none"

        mode = self._mem0_fallback_mode
        if mode == "local_only":
            self._try_init_mem0_local()
            return
        if mode == "local_then_http":
            if self._try_init_mem0_local():
                return
            self._try_init_mem0_http()
            return
        if mode == "http_then_local":
            if self._try_init_mem0_http():
                return
            self._try_init_mem0_local()
            return
        self._try_init_mem0_http()

    def _try_init_mem0_http(self) -> bool:
        try:
            client = _Mem0HTTPClient(
                api_url=self._mem0_api_url,
                api_key=self._mem0_api_key,
                timeout_s=self._mem0_timeout_s,
            )
            ok, detail = client.ping()
            if ok:
                self._mem0_client = client
                self._mem0_active_backend = "http"
                logger.info(
                    "Mem0 client initialized (backend=http, api_url=%s, user_id=%s)",
                    self._mem0_api_url,
                    self._mem0_user_id,
                )
                return True
            logger.warning("Mem0 HTTP init failed (unreachable): %s", detail)
            return False
        except Exception as exc:
            logger.warning("Mem0 HTTP init failed (non-fatal): %s", exc)
            return False

    def _try_init_mem0_local(self) -> bool:
        if not self._mem0_local_enabled:
            logger.info("Mem0 local backend disabled by config")
            return False
        try:
            if self._mem0_local_client is None:
                self._mem0_local_client = _Mem0LocalClient(
                    local_cfg=self._mem0_local_cfg,
                    timeout_s=self._mem0_timeout_s,
                )
            ok, detail = self._mem0_local_client.ping()
            if ok:
                self._mem0_client = self._mem0_local_client
                self._mem0_active_backend = "local"
                logger.info(
                    "Mem0 client initialized (backend=local, user_id=%s)",
                    self._mem0_user_id,
                )
                return True
            logger.warning("Mem0 local init failed (unreachable): %s", detail)
            return False
        except Exception as exc:
            logger.warning("Mem0 local init failed (non-fatal): %s", exc)
            return False

    def _maybe_failover_mem0(self, exc: Exception) -> bool:
        mode = self._mem0_fallback_mode
        if self._mem0_active_backend == "http" and mode == "http_then_local":
            logger.warning("Mem0 HTTP failed, trying local fallback: %s", exc)
            return self._try_init_mem0_local()
        if self._mem0_active_backend == "local" and mode == "local_then_http":
            logger.warning("Mem0 local failed, trying HTTP fallback: %s", exc)
            return self._try_init_mem0_http()
        return False

    def _mem0_query(self, query: str) -> List[str]:
        if not self._mem0_client:
            return []
        try:
            response = self._mem0_client.search(
                query=str(query or ""),
                user_id=self._mem0_user_id or "default",
                top_k=self._mem0_recall_max_results,
            )
        except Exception as exc:
            if self._maybe_failover_mem0(exc) and self._mem0_client:
                response = self._mem0_client.search(
                    query=str(query or ""),
                    user_id=self._mem0_user_id or "default",
                    top_k=self._mem0_recall_max_results,
                )
            else:
                raise
        rows = response.get("results", [])
        if not isinstance(rows, list):
            return []
        out: List[str] = []
        for row in rows[: self._mem0_recall_max_results]:
            text = ""
            if isinstance(row, dict):
                text = str(row.get("memory") or row.get("text") or "").strip()
            else:
                text = str(row).strip()
            if not text:
                continue
            if len(text) > 260:
                text = text[:260] + "..."
            out.append(text)
        return out

    def _mem0_retain_once(
        self,
        safe_user: str,
        safe_assistant: str,
        metadata: Dict[str, Any],
        source_key: str,
    ) -> Dict[str, Any]:
        if not self._mem0_client:
            return {"retained": False, "duplicate": False}
        payload_preview = f"{safe_user}\n{safe_assistant}".strip()
        retain_hash = self._build_mem0_retain_hash(payload_preview, source_key)

        with self._connect_index() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO mem0_retain_log
                (retain_hash, user_id, source_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    retain_hash,
                    self._mem0_user_id or "default",
                    source_key,
                    self._now_iso(),
                ),
            )
            conn.commit()
            if not cur.rowcount:
                return {"retained": False, "duplicate": True}

        messages: List[Dict[str, str]] = []
        if safe_user:
            messages.append({"role": "user", "content": safe_user})
        if safe_assistant:
            messages.append({"role": "assistant", "content": safe_assistant})
        if not messages:
            return {"retained": False, "duplicate": False}

        try:
            try:
                self._mem0_client.add_memory(
                    messages=messages,
                    user_id=self._mem0_user_id or "default",
                    metadata=metadata,
                )
            except Exception as exc:
                if self._maybe_failover_mem0(exc) and self._mem0_client:
                    self._mem0_client.add_memory(
                        messages=messages,
                        user_id=self._mem0_user_id or "default",
                        metadata=metadata,
                    )
                else:
                    raise
        except Exception:
            with self._connect_index() as conn:
                conn.execute("DELETE FROM mem0_retain_log WHERE retain_hash = ?", (retain_hash,))
                conn.commit()
            raise
        return {"retained": True, "duplicate": False}

    @staticmethod
    def _build_mem0_retain_hash(payload: str, source_key: str) -> str:
        raw = f"{source_key}|{payload}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _build_mem0_source_key(self, *, session_id: str, transcript: str) -> str:
        sid = str(session_id or "").strip()
        digest = hashlib.sha256(f"{sid}|{transcript}".encode("utf-8")).hexdigest()[:20]
        return f"mem0:{sid}:{digest}"

    def _clip_mem0_content(self, content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        if len(text) <= self._mem0_live_max_chars:
            return text
        return text[: self._mem0_live_max_chars] + "\n...[truncated by local_memory for mem0 retain]"

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

    def _ensure_hindsight_client_thread_compat(self) -> None:
        """Rebuild modern hindsight client when crossing thread boundaries.

        hindsight-client's modern implementation keeps an aiohttp session that is
        bound to the event loop/thread that first created it. Reusing that same
        client from another thread can fail with:
        "Timeout context manager should be used inside a task".
        """
        if not self._hindsight_client:
            return
        if self._hindsight_client_flavor != "modern":
            return

        current_tid = threading.get_ident()
        if self._hindsight_client_thread_id == current_tid:
            return

        try:
            from hindsight_client import Hindsight  # type: ignore
        except Exception as exc:
            logger.warning("Hindsight thread-compat import failed: %s", exc)
            return

        try:
            old_client = self._hindsight_client
            kwargs: Dict[str, Any] = {"base_url": self._hindsight_api_url, "timeout": self._hindsight_timeout}
            if self._hindsight_api_key:
                kwargs["api_key"] = self._hindsight_api_key
            self._hindsight_client = Hindsight(**kwargs)
            self._hindsight_client_thread_id = current_tid
            # Best-effort close to avoid leaked sessions; ignore loop affinity failures.
            try:
                close_fn = getattr(old_client, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Hindsight thread-compat rebuild failed: %s", exc)
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

    def _get_mem0_backfill_checkpoint(self) -> int:
        with self._connect_index() as conn:
            row = conn.execute(
                """
                SELECT last_backfill_turn_id
                FROM mem0_state
                WHERE user_id = ?
                """,
                (self._mem0_user_id or "default",),
            ).fetchone()
        if not row or row[0] is None:
            return 0
        return max(0, int(row[0]))

    def _set_mem0_backfill_checkpoint(self, turn_id: int) -> None:
        turn_id = max(0, int(turn_id))
        with self._connect_index() as conn:
            conn.execute(
                """
                INSERT INTO mem0_state (user_id, last_backfill_turn_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_backfill_turn_id = CASE
                        WHEN excluded.last_backfill_turn_id > mem0_state.last_backfill_turn_id
                        THEN excluded.last_backfill_turn_id
                        ELSE mem0_state.last_backfill_turn_id
                    END,
                    updated_at = excluded.updated_at
                """,
                (self._mem0_user_id or "default", turn_id, self._now_iso()),
            )
            conn.commit()

    def _build_backfill_episodes(
        self,
        raw_rows: List[Any],
        *,
        max_chars: int,
    ) -> List[Dict[str, Any]]:
        episodes: List[Dict[str, Any]] = []
        i = 0
        while i < len(raw_rows):
            row = raw_rows[i]
            row_id = int(row[0])
            sid = str(row[1] or "")
            role = str(row[2] or "").strip().lower()
            content = self._clip_hindsight_content(str(row[3] or ""), max_chars)
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
                assistant_content = self._clip_hindsight_content(str(next_row[3] or ""), max_chars)
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

    def _build_mem0_backfill_items(
        self,
        raw_rows: List[Any],
        *,
        max_chars: int,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        i = 0
        while i < len(raw_rows):
            row = raw_rows[i]
            row_id = int(row[0])
            sid = str(row[1] or "")
            role = str(row[2] or "").strip().lower()
            content = self._clip_mem0_content(str(row[3] or "")[:max_chars])
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
                assistant_content = self._clip_mem0_content(str(next_row[3] or "")[:max_chars])
                meta: Dict[str, Any] = {
                    "session_id": sid,
                    "source": "local_memory_mem0_backfill",
                    "source_turn_ids": f"{row_id},{aid}",
                    "created_at": created_at,
                }
                pair_text = f"User: {content}\nAssistant: {assistant_content}"
                pair_nsfw_tag, pair_nsfw_reason = self._classify_nsfw(pair_text)
                if pair_nsfw_tag:
                    meta["nsfw_tag"] = "1"
                    meta["nsfw_reason"] = pair_nsfw_reason
                items.append(
                    {
                        "source_key": f"mem0_backfill:pair:{row_id}:{aid}",
                        "safe_user": content,
                        "safe_assistant": assistant_content,
                        "metadata": meta,
                        "end_turn_id": aid,
                    }
                )
                i += 2
                continue

            safe_user = content if role != "assistant" else ""
            safe_assistant = content if role == "assistant" else ""
            meta = {
                "session_id": sid,
                "source": "local_memory_mem0_backfill",
                "source_turn_ids": str(row_id),
                "created_at": created_at,
                "source_role": role or "unknown",
            }
            single_nsfw_tag, single_nsfw_reason = self._classify_nsfw(content)
            if single_nsfw_tag:
                meta["nsfw_tag"] = "1"
                meta["nsfw_reason"] = single_nsfw_reason
            items.append(
                {
                    "source_key": f"mem0_backfill:turn:{row_id}",
                    "safe_user": safe_user,
                    "safe_assistant": safe_assistant,
                    "metadata": meta,
                    "end_turn_id": row_id,
                }
            )
            i += 1

        return items

    def _hindsight_query(self, query: str) -> str:
        if not self._hindsight_client:
            return ""
        self._ensure_hindsight_client_thread_compat()

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

    @staticmethod
    def _clip_hindsight_content(content: str, max_chars: int) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        head = text[:max_chars]
        return head + "\n...[truncated by local_memory for hindsight retain]"

    def _hindsight_retain(self, transcript: str, metadata: Dict[str, str]) -> None:
        if not self._hindsight_client:
            return
        self._ensure_hindsight_client_thread_compat()
        kwargs = {
            "content": transcript,
            "context": "conversation between Hermes Agent and the User",
            "metadata": metadata,
        }
        if self._hindsight_client_flavor == "legacy":
            kwargs["bank_id"] = self._hindsight_bank_id
            self._hindsight_client.retain(**kwargs)
            return
        self._hindsight_client.retain_batch(
            bank_id=self._hindsight_bank_id,
            items=[kwargs],
            retain_async=self._hindsight_retain_server_async,
        )

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
