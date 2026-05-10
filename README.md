# hermes-local-memory-provider

Upgrade-safe local memory provider for Hermes Agent.  
Hermes Agent 的升级安全本地记忆插件（sidecar memory provider）。

Current version / 当前版本: **v1.3.0**

## What This Solves / 解决什么问题

- Keeps Hermes core DB (`state.db`) read-only in this plugin path
- Stores all new index/state writes in sidecar SQLite
- Adds optional memory integrations with:
- Mem0 (HTTP or local library fallback)
- Hindsight
- Graphiti
- Reflector summaries
- Preserves NSFW-tagged records (store + label, no deletion by default)

## Key Features / 关键能力

- Read-only compatibility adapter for Hermes `state.db`
- Sidecar indexing in `memory_index.sqlite`
- Mem0 real-time retain/recall
- Mem0 local library mode (`local_only`) for no-Docker setups
- Mem0 failover modes:
- `http_only`
- `local_only`
- `http_then_local`
- `local_then_http`
- Mem0/Hindsight historical backfill with checkpoint + de-dup
- On-demand memory lane:
- `local_memory_ondemand_write`
- `local_memory_ondemand_recall`
- Independent notes KB:
- `local_notes_kb_import`
- `local_notes_kb_search`
- `local_notes_kb_sync`
- `local_notes_kb_status`

## Install (Auto Configure) / 自动安装与识别

### A) Default profile only / 仅默认 profile

```bash
cd ~/git/hermes-local-memory-provider
bash scripts/install.sh
```

### B) Default + specific profiles / 默认 + 指定 profile

```bash
cd ~/git/hermes-local-memory-provider
HERMES_PROFILES="yaoer,zhuer" bash scripts/install.sh
```

What installer does / 安装脚本做了什么:

- Copies plugin runtime files into `~/.hermes/plugins/local_memory/`
- Installs helpers under `~/.hermes/memory/` and `~/.hermes/tools/`
- Renders config from `examples/config.yaml` with `${HERMES_HOME}` replacement
- Auto sets Hermes config keys:
- `memory.provider=local_memory`
- `memory.local_memory.config_path=<resolved path>`
- For each profile in `HERMES_PROFILES`, does the same under `~/.hermes/profiles/<name>/`

## Local-Only Mem0 (No Docker) / Mem0 本地优先（无 Docker）

Recommended when Docker mem0 is disabled:

- Set `mem0.fallback_mode: local_only`
- Keep `mem0.api_url: ""`
- Configure local backend:
- LLM gateway (`openai` provider) with your base_url/model
- Embedding via local Ollama (`ollama` + `bge-m3`, 1024 dims)
- Use dedicated qdrant collection per profile to avoid dim conflicts

Reference guide:  
[docs/mem0-local-only.zh-en.md](docs/mem0-local-only.zh-en.md)

## Verify / 验证

```bash
python3 ~/.hermes/tools/upgrade_check.py --config ~/.hermes/plugins/local_memory/config.yaml
```

Optional profile check:

```bash
hermes --profile yaoer config show
```

## Upgrade Safety / 升级安全约束

- Never modifies Hermes core SQLite schema
- All new writes are sidecar-only
- Fail-open by default to avoid blocking chat flow

## Docs

- Re-add + structure (CN+EN):  
  [docs/reinstall-and-structure.zh-en.md](docs/reinstall-and-structure.zh-en.md)
- Local-only Mem0 guide (CN+EN):  
  [docs/mem0-local-only.zh-en.md](docs/mem0-local-only.zh-en.md)
- Rollback notes:  
  [docs/rollback-local-memory.md](docs/rollback-local-memory.md)
- Changelog (CN+EN):  
  [docs/changelog.zh-en.md](docs/changelog.zh-en.md)
