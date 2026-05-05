# hermes-local-memory-provider

Upgrade-safe local memory provider for Hermes Agent.

## Features

- Read-only access to Hermes `state.db` via compatibility adapter
- Sidecar indexing in `memory_index.sqlite`
- Optional Graphiti recall/sync adapter
- Optional Reflector summary worker
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
