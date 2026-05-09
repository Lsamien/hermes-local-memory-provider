# Changelog / 更新日志

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
