# 全量消息归档真实 Telegram QA 记录模板

> 这份文件是模板。真实验证时优先运行 `python -m tgwatch archive-qa-init --config config.toml`，生成到 gitignored 的 `reports/full_archive_qa/`。记录文件不要填写手机号、api_hash、session 路径、私密群名、真实用户昵称或未脱敏消息正文。

## 结论

- 验证日期：
- archive-qa-init 生成时 full_archive.enabled：
- tgwatch commit：
- 验证人：
- 结论：未通过 / 部分通过 / 通过
- 是否可以声明 full archive 真实端到端可交付：否 / 是

## 环境

- macOS 版本：
- Python 版本：
- tgwatch 启动方式：
- Telethon 版本：
- Telegram 测试群类型：普通群 / forum-enabled group
- full archive root_dir：已脱敏路径
- tracked DB：已脱敏路径
- archive-qa-init 生成时 capture_scope：
- archive-qa-init 生成时 topic_ids 数量：
- archive-qa-init 生成时 backfill_limit_messages：
- archive-qa-init 生成时 source_chat_id 状态：未配置 / 已配置且匹配 target / 已配置但不匹配 target

## 脱敏说明

- `api_hash`、手机号、session 文件路径：未记录 / 已脱敏
- 群 ID、用户 ID：已保留必要数字 / 已脱敏
- 测试消息正文：使用带时间戳的人工测试文本 / 已脱敏
- 媒体文件名：未使用 / 已脱敏

## 验证前状态

```bash
python -m tgwatch doctor --config config.toml
python -m tgwatch archive-status --config config.toml
```

- `doctor` 结论：
- `archive-status` 初始状态：disabled / empty / ok / degraded
- 如果是 disabled：停止验证，先把 `[full_archive] enabled = true`，再重新运行 `doctor` 和 `archive-status`：
- 如果是 degraded，原因和处理：

## Topic listing

```bash
python -m tgwatch list-topics --config config.toml --chat <target_chat_id>
```

- 是否执行：否 / 是
- 是否输出 Topic ID 和标题：
- General `1` 是否标记为 `whole_group`：
- 普通 Topic 是否标记为 `topic_ids`：
- 权限、entity、API 签名问题：

## 整群 live capture

配置摘要：

```toml
[full_archive]
enabled = true
source_chat_id = <target_chat_id>
capture_scope = "whole_group"
topic_ids = []
backfill_limit_messages = 0
```

执行：

```bash
python -m tgwatch run --config config.toml
python -m tgwatch archive-status --config config.toml
python -m tgwatch archive-context --config config.toml --chat <target_chat_id> --message-id <tracked_message_id> --before-minutes 10 --after-minutes 5
```

测试消息：

- 非 tracked 文本消息标识：
- tracked 文本消息标识：
- tracked reply / 媒体消息标识：
- service message 标识（如置顶、Topic 变更、成员变更；记录其 Topic / General / 未知归类，未验证则写明原因）：

通过条件：

- 非 tracked 消息进入 full archive：
- 非 tracked 消息没有进入 tracked DB：
- tracked 消息进入 tracked DB：
- full archive 中 tracked 消息为 `tracked_ref`：
- full archive 没有重复保存 tracked 正文或 archive 侧媒体元数据：
- service message 以 `message_kind = "service"` 进入 full archive，且不会影响 tracked-user 推送：
- `archive-context` 能看到 tracked 消息前后上下文：
- 控制群推送 / 报告 / realtime 未回归：

## Topic live capture

配置摘要：

```toml
[full_archive]
enabled = true
source_chat_id = <target_chat_id>
capture_scope = "topics"
topic_ids = [<topic_id>]
```

执行：

```bash
python -m tgwatch run --config config.toml
python -m tgwatch archive-status --config config.toml
python -m tgwatch archive-context --config config.toml --chat <target_chat_id> --message-id <tracked_message_id> --topic-id <topic_id>
```

Topic mismatch 诊断（选择一个已知属于其他 Topic 或 General 的 tracked message）：

```bash
python -m tgwatch archive-context --config config.toml --chat <target_chat_id> --message-id <other_topic_tracked_message_id> --topic-id <topic_id>
```

通过条件：

- 配置 Topic 内的非 tracked 消息被归档：
- 配置 Topic 内的 tracked 消息被归档并 relink：
- 配置 Topic 内的 service message 如发生，应以 `message_kind = "service"` 归档，并且 Topic / Reply 列能解释它归属该 Topic 的原因：
- 其他 Topic / General 的测试消息没有进入该 Topic 查询结果：
- 目标 tracked message 实际属于其他 Topic / General 时，`archive-context --topic-id` 输出 `Target topic mismatch` 并返回非零：
- `archive-context` 的 Topic / Reply 列与 Telegram UI 一致：
- 偏差或未验证项：

## Context / tracked DB 诊断

执行：

```bash
python -m tgwatch archive-context --config config.toml --chat <target_chat_id> --message-id <tracked_message_id> --before-minutes 10 --after-minutes 5
```

通过条件：

- 目标 tracked message 存在时，命令不会误报 `tracked message not found`：
- tracked DB 不可读、路径失效或缺少目标查询列时，CLI 输出 `cannot read tracked DB for target message`，而不是误报消息不存在：
- archive 中存在 `tracked_ref` 但 tracked DB 当前不可读时，上下文结果保留普通 archive rows，并标出 `could not read current tracked DB`：
- 旧 tracked DB schema 缺少正文列时，上下文结果保留普通 archive rows，并标出 schema 问题：
- 使用错误 `--topic-id` 查询目标 tracked message 时，CLI 输出 `Target topic mismatch`，且不把错误 Topic 的邻近消息当作目标上下文：
- 如果修复 tracked DB 路径或迁移 schema 后重试，`archive-context` 能重新解析 tracked 正文 / reply 摘要：

## Backfill

```bash
python -m tgwatch archive-backfill --config config.toml --limit 20 --dry-run
python -m tgwatch archive-backfill --config config.toml --limit 20 --apply
python -m tgwatch archive-status --config config.toml
python -m tgwatch archive-backfill --config config.toml --limit 20 --apply
python -m tgwatch archive-status --config config.toml
```

通过条件：

- dry-run 不写 archive rows：
- 第一次 apply 后 archive rows / tracked links 增长符合扫描结果：
- 第二次 apply 不重复增加 archive rows：
- 第二次 apply 不重复增加 manifest message count：
- 第二次 apply 不重复增加 tracked link rows：
- 重复消息计入 updated rows：
- 触发 FloodWait 时可恢复并降速：
- `archive-context` 能围绕历史 tracked message 读出 backfill 上下文：

## Sender 显示快照

执行 `--apply` 前先停止 watcher daemon，避免 primary session 被两个进程同时使用。

```bash
python -m tgwatch archive-senders-backfill --config config.toml --dry-run
python -m tgwatch archive-senders-backfill --config config.toml --limit 20 --apply
python -m tgwatch archive-senders-backfill --config config.toml --limit 20 --dry-run
python -m tgwatch archive-context --config config.toml --chat <target_chat_id> --message-id <tracked_message_id> --before-minutes 10 --after-minutes 5
```

通过条件：

- 第一次 dry-run 只统计缺少快照的 distinct sender，不连接 Telegram、不写 shard：
- 模拟旧 shard 仅缺少 `archive_senders` 表时，apply 会自行创建并继续；存在其他 degraded 条件时仍拒绝写入：
- apply 输出区分 session cache、Telegram history、unresolved 和 shard writes，且日志/证据不包含 raw sender ID：
- 同一 sender 在一次 apply 中最多查询一次；触发 FloodWait 时自动等待并继续：
- live sender lookup 瞬时失败时消息仍归档，cooldown 后的新消息会重新解析并补上 snapshot：
- 第二次 dry-run 的缺失数量按成功写入数下降；unresolved sender 不阻塞其他 sender：
- daemon 重启后，新的普通消息会自动写入 `archive_senders`，同一 sender 后续消息只扩展 first/last seen 时间：
- tracked message 仍是 `tracked_ref`，无重复正文/媒体 metadata，同时对应 sender snapshot 可用：
- `archive-context` 对 tracked 用户优先显示配置 alias，对其他成员显示 display name / `@username`，无法解析时显示匿名标签，任何情况都不显示 raw sender ID：

## 数据库可管理性

- 删除整个 `full_archive.root_dir` 后 tracked DB 是否仍可用：
- 删除整个 `full_archive.root_dir` 后 `archive-status` 是否为 empty、`archive-context` 是否只读返回空上下文、下一次 live/backfill 写入是否从新空归档重建：
- 重建后重新遇到 tracked 消息时是否仍为 `tracked_ref`，且没有 archive 侧 tracked 正文或媒体元数据：
- 删除 shard 后 `archive-status` 是否报告 missing shard：
- `archive-repair --dry-run` 是否给出可读修复计划：
- `archive-repair --prune-missing-shards --apply` 是否只删除 manifest stale rows：

## 最终风险记录

- FloodWait：
- Topic 归类偏差（尤其是 service message、Topic 变更、置顶、General 消息）：
- 权限 / entity 问题：
- SQLite 锁或云同步路径问题：
- 性能问题：
- 未验证项：

## 最终判定

- 本次真实 QA 是否通过：
- CR 结论：离线通过，待真实 QA / 本机可验收，待真实 QA / 真实端到端可交付
- CR 结论依据：不得只写“测试通过”；必须说明对应的 doctor、archive-status、archive-context、live capture、backfill 或未验证项证据
- 不通过或部分通过时，必须创建 / 更新 Todoist 后续任务：
- 可以进入 Release 的条件是否满足：
