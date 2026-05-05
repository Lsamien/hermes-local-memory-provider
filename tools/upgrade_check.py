#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.request import Request, urlopen

import yaml


class CheckResult:
    def __init__(self) -> None:
        self.items: List[Tuple[str, str]] = []

    def add(self, kind: str, message: str) -> None:
        self.items.append((kind, message))
        print(f"[{kind}] {message}")

    def final_code(self) -> int:
        has_fail = any(k == "FAIL" for k, _ in self.items)
        has_warn = any(k == "WARN" for k, _ in self.items)
        if has_fail:
            print("\nResult: INCOMPATIBLE")
            return 2
        if has_warn:
            print("\nResult: COMPATIBLE_WITH_WARNINGS")
            return 0
        print("\nResult: COMPATIBLE")
        return 0


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ensure_sys_path(hermes_home: Path) -> None:
    home_root = hermes_home
    hermes_agent_root = hermes_home / "hermes-agent"
    plugin_root = hermes_home / "plugins"
    for p in (home_root, hermes_agent_root, plugin_root):
        ps = str(p)
        if ps not in sys.path:
            sys.path.insert(0, ps)


def load_module_from_file(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec: {module_name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sqlite_readonly_check(db_path: Path) -> None:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")').fetchall()
    return {str(r[1]) for r in rows}


def sqlite_fts5_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
        if row and int(row[0]) == 1:
            return True
    except Exception:
        pass

    try:
        rows = conn.execute("PRAGMA compile_options").fetchall()
        opts = {str(r[0]).upper() for r in rows}
        return "ENABLE_FTS5" in opts
    except Exception:
        return False


def check_graphiti_endpoint(endpoint: str, timeout: float = 2.5) -> Tuple[bool, str]:
    payload = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "local-memory-upgrade-check", "version": "1.0"},
        },
        "id": 1,
    }
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = getattr(resp, "status", 200)
            if int(status) >= 400:
                return False, f"HTTP {status}"
            return True, "reachable"
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    hermes_home = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    ).expanduser().resolve()
    parser = argparse.ArgumentParser(description="Hermes local-memory upgrade compatibility check")
    parser.add_argument(
        "--config",
        default=str(hermes_home / "plugins" / "local_memory" / "config.yaml"),
        help="Path to local-memory config.yaml",
    )
    args = parser.parse_args()

    result = CheckResult()
    print("Hermes Local Memory Upgrade Check\n")

    config_path = Path(args.config).expanduser().resolve()
    try:
        cfg = load_yaml(config_path)
        result.add("PASS", f"Config loaded: {config_path}")
    except Exception as exc:
        result.add("FAIL", f"Config load failed: {exc}")
        return result.final_code()

    ensure_sys_path(hermes_home)

    hermes_sqlite = Path(str(cfg.get("hermes", {}).get("sqlite_path", ""))).expanduser().resolve()
    if not hermes_sqlite.exists():
        result.add("FAIL", f"Hermes SQLite not found: {hermes_sqlite}")
        return result.final_code()
    result.add("PASS", f"Hermes SQLite exists: {hermes_sqlite}")

    try:
        sqlite_readonly_check(hermes_sqlite)
        result.add("PASS", "Hermes SQLite read-only open succeeded")
    except Exception as exc:
        result.add("FAIL", f"Hermes SQLite read-only open failed: {exc}")
        return result.final_code()

    schema = cfg.get("hermes_schema", {})
    table = str(schema.get("table", ""))
    required = {
        str(schema.get("id_column", "")),
        str(schema.get("session_column", "")),
        str(schema.get("role_column", "")),
        str(schema.get("content_column", "")),
        str(schema.get("created_at_column", "")),
    }

    ro_conn = sqlite3.connect(f"file:{hermes_sqlite}?mode=ro", uri=True, timeout=5.0)
    try:
        if not sqlite_table_exists(ro_conn, table):
            result.add("FAIL", f"Configured table not found: {table}")
            return result.final_code()
        result.add("PASS", f"Table exists: {table}")

        cols = sqlite_columns(ro_conn, table)
        missing = sorted(c for c in required if c and c not in cols)
        if missing:
            result.add("FAIL", f"Missing configured columns: {missing}")
            return result.final_code()
        result.add("PASS", "Required columns exist")

        q = (
            f"SELECT \"{schema.get('id_column')}\", \"{schema.get('content_column')}\", "
            f"\"{schema.get('created_at_column')}\" FROM \"{table}\" "
            f"ORDER BY \"{schema.get('created_at_column')}\" DESC LIMIT 5"
        )
        rows = ro_conn.execute(q).fetchall()
        result.add("PASS", f"Recent messages readable: {len(rows)} rows")
    except Exception as exc:
        result.add("FAIL", f"Hermes SQLite schema/read check failed: {exc}")
        return result.final_code()
    finally:
        ro_conn.close()

    memory_index_path = Path(
        str(cfg.get("memory_index", {}).get("sqlite_path", ""))
    ).expanduser().resolve()
    try:
        memory_index_path.parent.mkdir(parents=True, exist_ok=True)
        idx_conn = sqlite3.connect(str(memory_index_path), timeout=5.0)
        result.add("PASS", f"memory_index.sqlite accessible: {memory_index_path}")
        if sqlite_fts5_enabled(idx_conn):
            result.add("PASS", "SQLite FTS5 available")
        else:
            result.add("FAIL", "SQLite FTS5 unavailable")
            return result.final_code()
        try:
            idx_cols = sqlite_columns(idx_conn, "memory_turns")
            if {"nsfw_tag", "nsfw_reason"}.issubset(idx_cols):
                result.add("PASS", "NSFW tag columns present in memory_turns")
            else:
                result.add("WARN", "NSFW tag columns missing in memory_turns")
        except Exception:
            result.add("WARN", "memory_turns schema not available for NSFW column check")
    except Exception as exc:
        result.add("FAIL", f"memory_index.sqlite check failed: {exc}")
        return result.final_code()
    finally:
        try:
            idx_conn.close()
        except Exception:
            pass

    plugin_dir = hermes_home / "plugins" / "local_memory"
    provider_path = plugin_dir / "__init__.py"
    orch_path = plugin_dir / "orchestrator.py"
    adapter_path = plugin_dir / "compatibility_adapter.py"

    for name, path in (
        ("local_memory_provider", provider_path),
        ("orchestrator", orch_path),
        ("compatibility_adapter", adapter_path),
    ):
        if not path.exists():
            result.add("FAIL", f"{name} missing: {path}")
            return result.final_code()

    try:
        from plugins.memory import load_memory_provider  # type: ignore

        provider = load_memory_provider("local_memory")
        if provider is None:
            result.add("FAIL", "local_memory_provider import failed via plugins.memory loader")
            return result.final_code()
        result.add("PASS", "local_memory_provider importable")
    except Exception as exc:
        result.add("FAIL", f"local_memory_provider import failed: {exc}")
        return result.final_code()

    for module_name in ("local_memory.orchestrator", "local_memory.compatibility_adapter"):
        try:
            importlib.import_module(module_name)
            result.add("PASS", f"{module_name.split('.')[-1]} importable")
        except Exception as exc:
            result.add("FAIL", f"{module_name.split('.')[-1]} import failed: {exc}")
            return result.final_code()

    graphiti_cfg = cfg.get("graphiti", {})
    graphiti_adapter_path = hermes_home / "memory" / "graphiti" / "adapter.py"
    graphiti_worker_path = hermes_home / "memory" / "graphiti" / "sync_worker.py"
    if graphiti_cfg.get("enabled", False):
        if not graphiti_adapter_path.exists():
            result.add("FAIL", f"Graphiti adapter missing: {graphiti_adapter_path}")
            return result.final_code()
        if not graphiti_worker_path.exists():
            result.add("FAIL", f"Graphiti sync worker missing: {graphiti_worker_path}")
            return result.final_code()
        try:
            importlib.import_module("memory.graphiti.adapter")
            importlib.import_module("memory.graphiti.sync_worker")
            result.add("PASS", "Graphiti adapter/worker importable")
        except Exception as exc:
            result.add("FAIL", f"Graphiti adapter import failed: {exc}")
            return result.final_code()
        endpoint = str(graphiti_cfg.get("endpoint", "")).strip()
        if not endpoint:
            result.add("WARN", "Graphiti enabled but endpoint missing")
        else:
            ok, detail = check_graphiti_endpoint(endpoint)
            if ok:
                result.add("PASS", "Graphiti endpoint reachable")
            else:
                result.add("WARN", f"Graphiti endpoint check failed: {detail}")
    else:
        if graphiti_adapter_path.exists() and graphiti_worker_path.exists():
            result.add("PASS", "Graphiti check skipped (disabled)")
        else:
            result.add("WARN", "Graphiti components not found (disabled)")

    reflector_cfg = cfg.get("reflector", {})
    reflector_worker_path = hermes_home / "memory" / "reflector" / "worker.py"
    if reflector_cfg.get("enabled", False):
        if not reflector_worker_path.exists():
            result.add("FAIL", f"Reflector worker missing: {reflector_worker_path}")
            return result.final_code()
        try:
            importlib.import_module("memory.reflector.worker")
            result.add("PASS", "Reflector worker importable")
        except Exception as exc:
            result.add("FAIL", f"Reflector worker import failed: {exc}")
            return result.final_code()
        trigger = reflector_cfg.get("trigger_every_n_turns")
        if isinstance(trigger, int) and trigger > 0:
            result.add("PASS", "Reflector config looks valid")
        else:
            result.add("WARN", "Reflector enabled but trigger_every_n_turns invalid")
    else:
        if reflector_worker_path.exists():
            result.add("PASS", "Reflector check skipped (disabled)")
        else:
            result.add("WARN", "Reflector worker not found (disabled)")

    nsfw_cfg = cfg.get("nsfw", {})
    if not isinstance(nsfw_cfg, dict):
        result.add("WARN", "NSFW config missing; defaults will be used")
    else:
        save_enabled = bool(nsfw_cfg.get("save", True))
        if save_enabled:
            result.add("PASS", "NSFW save enabled")
        else:
            result.add("WARN", "NSFW save disabled")

    return result.final_code()


if __name__ == "__main__":
    sys.exit(main())
