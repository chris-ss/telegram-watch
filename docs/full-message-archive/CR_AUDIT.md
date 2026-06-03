# 全量消息归档非真实 TG CR 审计

## 审计范围

本文件记录不依赖真实 Telegram 群组实跑的交付审计。真实 Telegram QA 仍由 `archive-qa-init` 生成的本地脱敏记录证明；在没有该记录时，CR 只能下结论为“离线通过，待真实 QA”或“本机可验收，待真实 QA”。

## 结论口径

- 当前非真实 TG 目标的 CR 口径：离线通过，待真实 QA。
- 不能声明：真实端到端可交付。
- 不能用单一 `pytest` 结果替代 CR。CR 必须逐项核对文档、实现、测试和验证命令。

## 要求与证据

| 要求 | 当前状态 | 证据 |
|------|----------|------|
| 文档、实现、测试一致 | 已覆盖，需每轮继续复核 | `README.md`、`DESIGN.md`、`ARCHITECTURE.md`、`DEVELOPMENT.md`、`TESTING.md` 同步描述 full archive 的默认关闭、分片、Topic、link、backfill、status、repair、context 和 CR 结论规则；对应测试覆盖配置、CLI、storage、runner、GUI。 |
| 现有 tracked-user watcher 不回归 | 已覆盖 | full archive 作为旁路注册；degraded preflight 时跳过 archive handler/relink，但仍注册 tracked handler；relink 后台执行，不阻塞 realtime queue。 |
| 默认关闭对老 config / 老 DB / 老用户无影响 | 已覆盖 | 缺少 `[full_archive]` 时默认 disabled；`config.example.toml` disabled；`archive-status` disabled 不建库；`doctor` 显示 disabled。 |
| 开启后保存指定群组或 Topic 上下文消息 | 离线覆盖，真实 TG 待 QA | `_FullArchiveHandler` 覆盖 NewMessage 和 MessageEdited；Topic scope 使用同一 `_archive_message_matches_scope`；backfill 与 live capture 复用同一归一化和 scope 判断。 |
| archive 数据与 tracked DB 建立清晰连接 | 已覆盖 | `tracked_ref` 行、`archive_tracked_links`、`tracked_db_links`、root-relative tracked DB path、moved project 兼容和 `archive-context` 解析均有测试。 |
| 不重复保存 tracked 正文和媒体 | 已覆盖 | relink 后清空 `archive_messages.text/raw_text`，删除 archive 侧 `archive_media`；status 能检测残留 payload/media；repair 能清理可安全修复的残留。 |
| 数据库可管理、可删除、可恢复 | 已覆盖 | 整删 `full_archive.root_dir` 后 status empty、context 只读空结果、下一次 live/backfill 重新建库；缺失 shard、孤儿 shard、未登记 shard、坏 schema、坏 link 均进入 degraded。 |
| backfill 安全 | 已覆盖 | 默认 dry-run；`--apply` 才写入；`--limit 0` 不连接 Telegram、不建库；degraded preflight 阻止 apply；FloodWait 后 sleep 并从 offset 继续。 |
| doctor/status/repair/context 可诊断 | 已覆盖 | `doctor` 检查目录、manifest、source target mismatch；`archive-status` 只读 health；`archive-repair` 默认 dry-run；`archive-context` 输出窗口、Target、Topic、Reply、tracked DB 错误和 skipped shard。 |
| 隐私和本地优先 | 已覆盖 | 不接入 AI/cloud；QA 草稿放入 gitignored reports；不打印 secret/session/API hash；status 和 context 诊断不打印本机 tracked DB 路径。 |

## 已记录风险

- Topic 归类是第一阶段 best-effort：writer 不维护 Topic metadata cache。`archive-context` 会输出 `topic_id`、`reply_to_msg_id`、`reply_to_top_id`，真实 Telegram QA 需要验证 forum 群组里的特殊消息归类。
- 真实 Telegram FloodWait 行为只能在真实账号环境最终确认；离线测试覆盖了 FloodWait 分支和恢复策略。
- SQLite 性能测试覆盖 10,000 行窗口查询；100,000 / 500,000 行属于本地 benchmark，按 release 时间和磁盘条件执行。
- 真实端到端可交付仍需要 gitignored QA 记录证明，不属于本审计文件的完成声明。

## 每轮 CR 最小检查

每轮结束前至少运行：

```bash
python -m pytest tests/ -q
git diff --check
diff -u AGENTS.md CLAUDE.md
python -m tgwatch doctor --config config.example.toml
python -m tgwatch archive-status --config config.example.toml
```

如果本机存在 `config.toml`，还应运行：

```bash
python -m tgwatch doctor --config config.toml
python -m tgwatch archive-status --config config.toml
```

当本机 `full_archive.enabled=false` 时，上述真实配置检查只能证明默认关闭和老路径无影响，不能证明 live Telegram capture。
