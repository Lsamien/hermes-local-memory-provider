# Local Memory Plugin Re-Add Guide (中文 + English)

> Version / 版本: v1.3.0

## 1) 目标 / Goal

中文：  
这份文档用于“重新添加（re-add）`local_memory` 插件”，并给出仓库结构与运行时结构说明。目标是让安装、升级、回滚、排障都可重复执行。

English:  
This document explains how to re-add the `local_memory` plugin and provides both repository and runtime structure details. The goal is a repeatable flow for install, upgrade, rollback, and troubleshooting.

---

## 2) 前置条件 / Prerequisites

中文：
- Hermes 已安装，默认目录：`~/.hermes`
- 本仓库已拉取到本地：`~/git/hermes-local-memory-provider`
- 若启用 Hindsight（`local_external`），请确保 API 可访问（常见为 `http://127.0.0.1:8882`）
- 若 Mem0 使用 HTTP 模式，请确保 Mem0 API 可访问（常见为 `http://127.0.0.1:18888`）
- 若 Mem0 使用本地模式（`local_only`），无需 Docker Mem0，但需本地 Python 依赖与 Ollama embedding 模型准备完毕

English:
- Hermes is installed (default home: `~/.hermes`)
- This repo is cloned locally at `~/git/hermes-local-memory-provider`
- If Hindsight is enabled in `local_external` mode, ensure the API is reachable (commonly `http://127.0.0.1:8882`)
- If Mem0 uses HTTP mode, ensure Mem0 API is reachable (commonly `http://127.0.0.1:18888`)
- If Mem0 uses local mode (`local_only`), Mem0 Docker is not required, but local Python deps and Ollama embedding model must be ready

---

## 3) 重新添加步骤 / Re-Add Steps

### 3.1 备份当前配置（建议） / Backup current config (recommended)

```bash
cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak.$(date +%Y%m%d_%H%M%S)
```

### 3.2 重新安装插件文件 / Reinstall plugin files

```bash
cd ~/git/hermes-local-memory-provider
bash scripts/install.sh
```

该脚本会同步以下内容到 `~/.hermes`：  
This script syncs the following into `~/.hermes`:
- `plugins/local_memory/*.py` + `plugin.yaml`
- `memory/graphiti/*.py`
- `memory/reflector/*.py`
- `tools/upgrade_check.py`
- `docs/rollback-local-memory.md`
- 生成 `~/.hermes/plugins/local_memory/config.yaml`（由 `examples/config.yaml` 填充）

### 3.3 设置 Hermes 主配置 / Configure Hermes root config

确保 `~/.hermes/config.yaml` 含以下关键项：  
Ensure `~/.hermes/config.yaml` contains these key entries:

```yaml
memory:
  provider: local_memory
  local_memory:
    config_path: /Users/<you>/.hermes/plugins/local_memory/config.yaml
  mem0:
    enabled: true
    api_url: http://127.0.0.1:18888
    user_id: zhuer
    explicit_query_only: true
  hindsight:
    enabled: true
    mode: local_external
    api_url: http://127.0.0.1:8882
    bank_id: zhuer
    recall_budget: mid
    recall_method: recall
    recall_max_results: 4
    retain_async: true
    retain_every_n_turns: 1
```

> 说明 / Note:  
> 现在插件会叠加读取 root `memory.hindsight` 覆盖项（例如 `bank_id`、`api_url`），无需只改插件默认配置。

### 3.4 多 profile 隔离（可选） / Multi-profile isolation (optional)

例如 `yaoer` profile：  
For `yaoer` profile:

```yaml
memory:
  provider: local_memory
  local_memory:
    config_path: /Users/<you>/.hermes/profiles/yaoer/plugins/local_memory/config.yaml
  hindsight:
    enabled: true
    mode: local_external
    api_url: http://127.0.0.1:8882
    bank_id: yaoer
```

### 3.5 重启 Hermes / Restart Hermes

重新启动 gateway/agent 进程使配置生效。  
Restart your Hermes gateway/agent processes to apply the new configuration.

---

## 4) 验证清单 / Validation Checklist

### 4.1 插件加载 / Plugin load

在日志确认 `local_memory` 注册成功。  
Check logs for successful `local_memory` registration.

### 4.2 Hindsight API 健康检查 / Hindsight API health check

```bash
curl -sS http://127.0.0.1:8882/health
```

期望 / Expected: `{"status":"healthy", ...}`

### 4.3 Bank 是否存在 / Verify bank exists

```bash
curl -sS http://127.0.0.1:8882/v1/default/banks
```

检查目标 `bank_id`（如 `zhuer` / `yaoer`）是否在返回列表中。  
Ensure target `bank_id` (e.g. `zhuer` / `yaoer`) exists in the response.

### 4.4 历史回填到 Hindsight / Historical backfill into Hindsight

v1.1.0 新增工具：`local_memory_hindsight_backfill`。  
New in v1.1.0: `local_memory_hindsight_backfill`.

用途 / Purpose:
- 把 `local_memory` sidecar 中的历史对话补写到 Hindsight
- 支持断点续传（checkpoint）
- 支持去重（de-dup），避免重复 retain

建议顺序 / Recommended flow:
1. 先 dry-run：`{"dry_run": true, "max_items": 500}`
2. 再正式回填：`{"dry_run": false, "max_items": 2000}`
3. 全量重扫可加：`{"force": true}`

### 4.5 历史回填到 Mem0 / Historical backfill into Mem0

v1.2.0 新增工具：`local_memory_mem0_backfill`。  
New in v1.2.0: `local_memory_mem0_backfill`.

用途 / Purpose:
- 把 `local_memory` sidecar 中的历史对话补写到 Mem0
- 支持断点续传（checkpoint）
- 支持去重（de-dup），避免重复 retain

建议顺序 / Recommended flow:
1. 先 dry-run：`{"dry_run": true, "max_items": 500}`
2. 再正式回填：`{"dry_run": false, "max_items": 2000}`
3. 全量重扫可加：`{"force": true}`

### 4.6 Mem0 本地优先模式 / Mem0 local-only mode

如果你不希望启动 Mem0 Docker，可使用 `local_only`：  
If you do not want to run Mem0 Docker, use `local_only`:

```yaml
mem0:
  enabled: true
  api_url: ""
  fallback_mode: local_only
  local_backend:
    enabled: true
    llm_provider: openai
    llm_base_url: http://127.0.0.1:3000/grok-web/v1
    llm_model: grok-4.20-fast
    llm_api_key: local-placeholder
    embedder_provider: ollama
    embedder_model: bge-m3
    embedding_dims: 1024
    embedder_base_url: http://127.0.0.1:11434
```

更多详细说明：  
See full guide:  
`docs/mem0-local-only.zh-en.md`

---

## 5) 仓库结构 / Repository Structure

```text
hermes-local-memory-provider/
├── plugins/
│   └── local_memory/
│       ├── __init__.py
│       ├── compatibility_adapter.py
│       ├── notes_kb.py
│       ├── orchestrator.py
│       └── plugin.yaml
├── memory/
│   ├── __init__.py
│   ├── graphiti/
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   └── sync_worker.py
│   └── reflector/
│       ├── __init__.py
│       ├── prompts.py
│       └── worker.py
├── tools/
│   └── upgrade_check.py
├── examples/
│   └── config.yaml
├── scripts/
│   └── install.sh
└── docs/
    ├── changelog.zh-en.md
    ├── rollback-local-memory.md
    └── reinstall-and-structure.zh-en.md
```

核心职责 / Key responsibilities:
- `plugins/local_memory/compatibility_adapter.py`  
  只读访问 Hermes `state.db`，做字段兼容映射。
- `plugins/local_memory/orchestrator.py`  
  记忆编排：sidecar 索引、Graphiti/Hindsight/Reflector 聚合。
- `plugins/local_memory/notes_kb.py`  
  独立笔记知识库导入/检索/增量同步（与对话记忆分离）。
- `memory/graphiti/*`  
  Graphiti MCP 召回与异步写入适配。
- `memory/reflector/*`  
  反思摘要（summary）能力。
- `tools/upgrade_check.py`  
  升级兼容检查工具。

---

## 6) 运行时结构 / Runtime Structure (`~/.hermes`)

```text
~/.hermes/
├── config.yaml
├── state.db                         # Hermes core DB (read-only for this plugin)
├── plugins/
│   └── local_memory/
│       ├── __init__.py
│       ├── compatibility_adapter.py
│       ├── notes_kb.py
│       ├── orchestrator.py
│       ├── plugin.yaml
│       └── config.yaml
├── memory/
│   ├── __init__.py
│   ├── graphiti/
│   ├── reflector/
│   ├── local_memory/
│       ├── memory_index.sqlite      # sidecar memory index
│       └── reflector.sqlite
│   └── notes_kb/
│       └── notes_kb.sqlite
└── tools/
    └── upgrade_check.py
```

关键原则 / Core principles:
- 不改 Hermes 原始库结构 / Do not alter Hermes core DB schema
- 新增写入走 sidecar / New writes go to sidecar stores
- 失败默认 fail-open，不阻塞主对话 / Fail-open by default; chat should continue

---

## 7) 常见问题 / Common Issues

### Q1: Bank 创建失败（如 `zhuer`） / Bank creation fails

排查顺序 / Check in order:
1. `api_url` 端口是否正确（很多环境是 `8882`，不是 `8888`）  
2. `memory.hindsight.bank_id` 是否写在 root/profile 配置  
3. Hermes 是否已重启  
4. `curl http://127.0.0.1:8882/v1/default/banks` 是否可返回 JSON

### Q2: 召回经常空 / Recall often empty

通常先看：
- Hindsight/Graphiti 服务可用性
- 查询语句是否过于特殊（FTS 语法敏感字符）
- 当前 profile 是否使用了预期 `bank_id`

---

## 8) 回滚 / Rollback

参考：`docs/rollback-local-memory.md`

简要：  
1. 还原 `~/.hermes/config.yaml` 备份  
2. 重启 Hermes  
3. sidecar 数据库保留（可按需清理）
