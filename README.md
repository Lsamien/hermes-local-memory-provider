# hermes-local-memory-provider

Upgrade-safe local memory provider for Hermes Agent.
Hermes Agent 的升级安全本地记忆插件（sidecar memory provider）。

Current version / 当前版本: **v1.2.0**

## Features

- Read-only access to Hermes `state.db` via compatibility adapter
- Sidecar indexing in `memory_index.sqlite`
- Optional Graphiti recall/sync adapter
- Optional Mem0 recall/retain adapter
- Optional Hindsight recall/retain adapter
- Optional Reflector summary worker
- Historical backfill to Hindsight with checkpoint + de-dup (`local_memory_hindsight_backfill`)
- Historical backfill to Mem0 with checkpoint + de-dup (`local_memory_mem0_backfill`)
- On-demand memory lane (`local_memory_ondemand_write` / `local_memory_ondemand_recall`)
- Independent notes KB (`local_notes_kb_import` / `local_notes_kb_search` / `local_notes_kb_sync`)
- NSFW tag preservation with configurable keyword lists
- Upgrade compatibility check script

## Layout

- `plugins/local_memory/` provider + orchestrator + compatibility adapter
- `memory/graphiti/` Graphiti adapter + sync worker
- `memory/reflector/` Reflector worker
- `tools/upgrade_check.py` compatibility check
- `examples/config.yaml` template config
- `docs/rollback-local-memory.md` rollback notes

## Install

```bash
cd ~/git/hermes-local-memory-provider
bash scripts/install.sh
```

Then set in Hermes:

```bash
hermes config set memory.provider local_memory
```

## Check

```bash
python3 ~/.hermes/tools/upgrade_check.py --config ~/.hermes/plugins/local_memory/config.yaml
```

## Notes

- This project never alters Hermes core SQLite schema.
- All new writes are sidecar-only.

## Re-Add Guide (CN + EN)

- 中文+English 详细“重新添加说明 + 目录结构”：  
  [docs/reinstall-and-structure.zh-en.md](docs/reinstall-and-structure.zh-en.md)

## Changelog (CN + EN)

- v1.2.0 / v1.1.0 release notes:  
  [docs/changelog.zh-en.md](docs/changelog.zh-en.md)
