# Changelog / 更新日志

## v1.2.0 (2026-05-10)

### 中文
- 新增 Mem0 实时桥接：支持在 `local_memory` 中进行 Mem0 retain/recall（可配置显式召回关键词）。
- 新增 Mem0 历史回填工具：`local_memory_mem0_backfill`（支持 checkpoint、去重、dry-run、force）。
- 新增 `memory.mem0` root 配置覆盖合并逻辑，便于按 profile 隔离 `user_id`。
- 增强 Hindsight 稳定性：修复现代客户端在异步/跨线程场景下常见的 `Timeout context manager should be used inside a task` 问题（线程兼容重建 + retain_batch 路径）。
- 增强 NSFW 关键词策略：英文关键词采用词边界匹配，降低误判；保留“可保存、不删除”策略。
- 增加写入内容长度裁剪保护，避免超长内容在回填/实时写入中导致不稳定。

### English
- Added live Mem0 bridge in `local_memory` with Mem0 retain/recall (including explicit-recall keyword control).
- Added Mem0 historical backfill tool: `local_memory_mem0_backfill` (checkpoint, de-dup, dry-run, force).
- Added root `memory.mem0` override merge support for easier profile-level `user_id` isolation.
- Improved Hindsight reliability for modern client async/thread cases, including mitigation for `Timeout context manager should be used inside a task` (thread-compat rebuild + retain_batch path).
- Improved NSFW keyword policy: boundary-based matching for English keywords to reduce false positives while keeping preserve-first behavior.
- Added content-length clipping safeguards for live retain and backfill stability.

## v1.1.0 (2026-05-09)

### 中文
- 新增 Hindsight 历史回填工具：`local_memory_hindsight_backfill`。
- 新增回填断点状态（checkpoint）与写入去重日志，降低重复写入风险。
- `retain_every_n_turns` 配置已接入实时写入流程（之前仅配置未生效）。
- 新增/完善 on-demand 记忆能力：
  - `local_memory_ondemand_write`
  - `local_memory_ondemand_recall`
- 新增/完善独立笔记知识库能力：
  - `local_notes_kb_import`
  - `local_notes_kb_search`
  - `local_notes_kb_sync`
  - `local_notes_kb_status`
- NSFW 标签策略保持“可保存、不删除”，并保留显式检索控制。
- 补充中英安装/重装说明，增加 v1.1.0 的回填验证步骤。

### English
- Added historical backfill tool for Hindsight: `local_memory_hindsight_backfill`.
- Added backfill checkpoint state and retain de-dup log to reduce duplicate writes.
- Wired `retain_every_n_turns` into live retain flow (previously configured but not enforced).
- Added/enhanced on-demand memory lane:
  - `local_memory_ondemand_write`
  - `local_memory_ondemand_recall`
- Added/enhanced independent Notes KB tools:
  - `local_notes_kb_import`
  - `local_notes_kb_search`
  - `local_notes_kb_sync`
  - `local_notes_kb_status`
- NSFW policy remains preserve-first (store/label, not delete) with explicit recall controls.
- Updated CN+EN reinstall docs with v1.1.0 backfill validation steps.
