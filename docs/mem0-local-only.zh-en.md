# Mem0 Local-Only Mode (CN + EN)

> Version / 版本: v1.3.0

## 1) 适用场景 / When to Use

中文：
- 你暂时不想启动 Mem0 Docker 容器
- 你希望记忆系统默认走本地 Python library
- 你仍然要保留 Hermes 主库只读、sidecar 可写原则

English:
- You do not want to run Mem0 Docker right now
- You want memory to use local Python library by default
- You still want Hermes core DB read-only + sidecar writes only

---

## 2) 核心结论 / Core Behavior

中文：
- `fallback_mode: local_only` 时，插件不会尝试 HTTP Mem0 API。
- 记忆 retain/recall 通过 `mem0ai` 本地库完成。
- 本地 embedding 可走 Ollama（例如 `bge-m3`）。

English:
- With `fallback_mode: local_only`, plugin does not use HTTP Mem0 API.
- Retain/recall is handled by local `mem0ai` library.
- Local embedding can use Ollama (e.g. `bge-m3`).

---

## 3) 推荐配置 / Recommended Config

File:
- `~/.hermes/plugins/local_memory/config.yaml`
- (optional profile) `~/.hermes/profiles/<profile>/plugins/local_memory/config.yaml`

```yaml
mem0:
  enabled: true
  api_url: ""
  api_key: ""
  user_id: "default"
  timeout_ms: 6000
  recall_max_results: 3
  fallback_mode: "local_only" # http_only | local_only | http_then_local | local_then_http
  explicit_query_only: true
  explicit_query_keywords:
    - "回忆"
    - "记得"
    - "历史"
    - "上次"
    - "之前"
    - "remember"
    - "recall"
    - "history"
    - "memory"
  retain_async: true
  live_max_content_chars: 3000
  local_backend:
    enabled: true
    llm_provider: "openai"
    llm_base_url: "http://127.0.0.1:3000/grok-web/v1"
    llm_model: "grok-4.20-fast"
    llm_api_key: "local-placeholder"
    embedder_provider: "ollama"
    embedder_model: "bge-m3"
    embedding_dims: 1024
    vector_store_provider: "qdrant"
    storage_path: "${HERMES_HOME}/memory/local_memory/mem0_local_qdrant"
    collection_name: "mem0_local_default_bgem3_1024"
    embedder_api_key: ""
    embedder_base_url: "http://127.0.0.1:11434"
```

Important / 重点:
- 每个 profile 建议使用不同 `collection_name`，避免跨 profile 冲突。
- `embedding_dims` 必须和 embedding 模型实际维度一致。

---

## 4) 依赖与安装 / Dependencies

Installer already declares these pip dependencies:
- `hindsight-client>=0.5.0`
- `mem0ai[nlp]>=2.0.0`
- `fastembed>=0.8.0`
- `ollama>=0.6.0`

If needed manually:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python "mem0ai[nlp]" fastembed ollama
```

Ollama model:

```bash
ollama pull bge-m3
```

---

## 5) 自动安装与识别 / Auto Install & Recognition

默认安装：

```bash
cd ~/git/hermes-local-memory-provider
bash scripts/install.sh
```

含 profile：

```bash
cd ~/git/hermes-local-memory-provider
HERMES_PROFILES="yaoer,zhuer" bash scripts/install.sh
```

脚本会自动：
- 同步插件文件到目标 home/profile
- 渲染 `plugins/local_memory/config.yaml`
- 设置 `memory.provider=local_memory`
- 设置 `memory.local_memory.config_path=<target>/plugins/local_memory/config.yaml`

---

## 6) 验证步骤 / Validation

1. 关闭 Mem0 Docker（如有）
2. 重启 Hermes gateway
3. 检查 provider

```bash
hermes config show
```

4. 运行升级检查

```bash
python3 ~/.hermes/tools/upgrade_check.py --config ~/.hermes/plugins/local_memory/config.yaml
```

5. 可选：脚本级初始化检查（应看到 local backend）

```bash
PYTHONPATH=~/.hermes/plugins ~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import yaml, copy
from local_memory.orchestrator import MemoryOrchestrator
cfg = yaml.safe_load(open('~/.hermes/plugins/local_memory/config.yaml'.replace('~','/Users/' + __import__('getpass').getuser()),'r',encoding='utf-8'))
cfg['graphiti']={'enabled':False}; cfg['reflector']={'enabled':False}; cfg['hindsight']={'enabled':False}
orc = MemoryOrchestrator(copy.deepcopy(cfg)); orc.initialize()
print(getattr(orc, '_mem0_active_backend', '<missing>'))
PY
```

Expected / 预期:
- `local`

---

## 7) 故障排查 / Troubleshooting

### A) `Mem0 HTTP init failed`

在 `local_only` 模式下可忽略旧日志痕迹；关键看当前实例是否 `backend=local`。

### B) embedding 维度冲突

现象：
- `shapes ... not aligned`

处理：
- 新建 `collection_name` 或 `storage_path`
- 确认 `embedding_dims` 与模型维度一致

### C) `The 'ollama' library is required`

安装 `ollama` Python 包到 Hermes venv（见上文依赖）。

### D) LLM 抽取报错（401/500）

插件已包含 `infer=false` 二次兜底（尽量保证落库）。  
建议修正你的 LLM 网关 key/model 配置，减少抽取失败率。

---

## 8) 回切 Docker / Switch Back to Docker Later

以后电脑升级后若恢复 Docker Mem0：
- 把 `fallback_mode` 改为 `http_then_local`
- 设置 `api_url`（例如 `http://127.0.0.1:18888`）
- 保留 `local_backend` 作为兜底

Then restart Hermes gateway.

