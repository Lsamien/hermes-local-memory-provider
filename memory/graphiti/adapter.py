from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass
class GraphitiConfig:
    endpoint: str
    group_id: str
    timeout_ms: int = 600
    recall_max_results: int = 5


class GraphitiAdapter:
    """Thin, core-independent adapter around Graphiti MCP HTTP calls."""

    def __init__(self, config: GraphitiConfig):
        self.config = config
        self._session_id: Optional[str] = None
        self._request_id = 0

    def ping(self) -> Tuple[bool, str]:
        try:
            self._initialize_session()
            return True, "reachable"
        except Exception as exc:
            return False, str(exc)

    def recall(self, query: str, max_results: Optional[int] = None) -> List[str]:
        if not query.strip():
            return []
        limit = int(max_results or self.config.recall_max_results)
        limit = max(1, min(limit, 20))

        facts = self._call_tool(
            "search_memory_facts",
            {
                "query": query,
                "group_ids": [self.config.group_id],
                "max_facts": limit,
            },
        )
        nodes = self._call_tool(
            "search_nodes",
            {
                "query": query,
                "group_ids": [self.config.group_id],
                "max_nodes": limit,
            },
        )

        lines: List[str] = []
        lines.extend(self._flatten_result(facts, "fact"))
        lines.extend(self._flatten_result(nodes, "node"))

        dedup: List[str] = []
        seen = set()
        for item in lines:
            norm = item.strip()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            dedup.append(norm)
        return dedup[:limit]

    def add_episode(
        self,
        name: str,
        episode_body: str,
        source_description: str = "local_memory_sync",
    ) -> Dict[str, Any]:
        return self._call_tool(
            "add_memory",
            {
                "name": name,
                "episode_body": episode_body,
                "group_id": self.config.group_id,
                "source": "text",
                "source_description": source_description,
            },
        )

    def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._initialize_session()
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
            "id": self._next_id(),
        }
        try:
            resp, _ = self._post(payload, include_session=True)
        except Exception:
            # Session may have expired; re-initialize once and retry.
            self._session_id = None
            self._initialize_session()
            payload["id"] = self._next_id()
            resp, _ = self._post(payload, include_session=True)
        if isinstance(resp, dict) and "error" in resp:
            raise RuntimeError(str(resp["error"]))
        if isinstance(resp, dict) and "result" in resp:
            return resp["result"] if isinstance(resp["result"], dict) else {"raw": resp["result"]}
        return resp if isinstance(resp, dict) else {"raw": resp}

    def _initialize_session(self) -> None:
        if self._session_id:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "local-memory-graphiti-adapter", "version": "1.0"},
            },
            "id": self._next_id(),
        }
        _, headers = self._post(payload, include_session=False)
        sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id") or headers.get("MCP-Session-Id")
        if not sid:
            raise RuntimeError("Graphiti MCP initialize returned no mcp-session-id")
        self._session_id = str(sid)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _post(self, payload: Dict[str, Any], include_session: bool) -> Tuple[Dict[str, Any], Dict[str, str]]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if include_session and self._session_id:
            headers["mcp-session-id"] = self._session_id

        req = Request(
            self.config.endpoint,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        timeout = max(float(self.config.timeout_ms) / 1000.0, 0.1)
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = resp.read().decode("utf-8", errors="replace")
            resp_headers = {k: v for k, v in resp.headers.items()}

        # MCP streamable HTTP often returns SSE-like lines: data: {...}
        if "data:" in body:
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if not payload_text:
                    continue
                try:
                    return json.loads(payload_text), resp_headers
                except Exception:
                    continue

        # Fallback plain JSON body
        try:
            return json.loads(body), resp_headers
        except Exception:
            logger.debug("Graphiti non-JSON response: %s", body[:500])
            return {"raw": body}, resp_headers

    @staticmethod
    def _flatten_result(result: Dict[str, Any], prefix: str) -> List[str]:
        items: List[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                txt = value.strip().replace("\n", " ")
                if txt:
                    items.append(f"[{prefix}] {txt}")
                return
            if isinstance(value, dict):
                for v in value.values():
                    walk(v)
                return
            if isinstance(value, list):
                for v in value:
                    walk(v)

        walk(result)
        return items
