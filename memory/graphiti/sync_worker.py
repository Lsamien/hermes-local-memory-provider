from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from .adapter import GraphitiAdapter

logger = logging.getLogger(__name__)


class GraphitiSyncWorker:
    """Independent sync worker that mirrors turns into Graphiti."""

    def __init__(self, adapter: GraphitiAdapter, async_write: bool = True):
        self.adapter = adapter
        self.async_write = bool(async_write)

    def sync_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        payload = self._build_episode_body(session_id, user_message, assistant_message, metadata or {})

        if self.async_write:
            t = threading.Thread(
                target=self._sync_safe,
                args=(session_id, payload),
                daemon=True,
                name="local-memory-graphiti-sync",
            )
            t.start()
            return

        self._sync_safe(session_id, payload)

    def _sync_safe(self, session_id: str, payload: str) -> None:
        try:
            self.adapter.add_episode(
                name=f"session_{session_id}",
                episode_body=payload,
                source_description="local_memory_graphiti_sync",
            )
        except Exception as exc:
            logger.warning("Graphiti sync failed: %s", exc)

    @staticmethod
    def _build_episode_body(
        session_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Dict[str, Any],
    ) -> str:
        parts = [
            f"session_id: {session_id}",
            f"user: {user_message.strip()}",
            f"assistant: {assistant_message.strip()}",
        ]
        if metadata:
            parts.append(f"metadata: {metadata}")
        return "\n".join(parts)
