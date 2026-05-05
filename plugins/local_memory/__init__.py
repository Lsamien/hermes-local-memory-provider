"""Local memory provider plugin.

Adapter-only provider layer:
- maps Hermes MemoryProvider lifecycle into the local orchestrator
- keeps business logic in orchestrator.py
- reads Hermes SQLite via compatibility_adapter.py in read-only mode
- fail-open by default so chat is never blocked by memory failures
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)


class LocalMemoryProvider(MemoryProvider):
    def __init__(self):
        self._session_id = ""
        self._hermes_home = get_hermes_home()
        self._fail_open = True
        self._orchestrator: Optional[MemoryOrchestrator] = None
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
        try:
            orchestrator.initialize()
        except Exception as exc:
            if self._fail_open:
                logger.warning("local_memory initialize failed (fail-open): %s", exc)
                self._orchestrator = None
                return
            raise

        self._orchestrator = orchestrator

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def system_prompt_block(self) -> str:
        if not self._orchestrator:
            return ""
        return (
            "# Local Memory Provider\n"
            "Local sidecar memory is enabled with fail-open mode. "
            "Core Hermes SQLite remains read-only for this provider."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._orchestrator:
            return ""
        try:
            return self._orchestrator.prefetch(
                query=query,
                context={"session_id": session_id or self._session_id},
            )
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

        return cfg


def register(ctx):
    ctx.register_memory_provider(LocalMemoryProvider())
