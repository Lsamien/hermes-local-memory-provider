# local_memory 回滚说明（不改 Hermes 基础库）

## 目标
在 local_memory 插件异常时，快速恢复到原 provider，且不影响 `state.db`。

## 步骤
1. 切回原 provider（你当前是 graphiti）：

```bash
hermes config set memory.provider graphiti
```

2. 如需彻底禁用外部 provider：

```bash
hermes config unset memory.provider
```

3. 停止本插件相关后台任务（如果你后续有自建 worker）：

```bash
pkill -f local_memory || true
```

4. 保留 sidecar（推荐）：

```text
/Users/samien/.hermes/memory/local_memory/memory_index.sqlite
```

5. 可选删除 sidecar（不会影响 Hermes 基础对话）：

```bash
rm -f /Users/samien/.hermes/memory/local_memory/memory_index.sqlite
```

## 验证
1. `state.db` 仍存在；
2. Hermes 对话功能正常；
3. 不再加载 `local_memory` provider。

## 重新启用
1. 先检查：

```bash
python3 /Users/samien/.hermes/tools/upgrade_check.py --config /Users/samien/.hermes/plugins/local_memory/config.yaml
```

2. 再启用：

```bash
hermes config set memory.provider local_memory
```
