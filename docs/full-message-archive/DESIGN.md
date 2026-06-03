# 全量消息归档设计文档

## 目标

新增一个可选的全量消息归档功能，在保留现有 tracked-user watcher 作为主产品逻辑的同时，为指定 Telegram 群组范围保存完整上下文。

## 非目标

- 不改变 tracked-user 采集、报告生成、媒体处理、控制群推送。
- 第一阶段不加入 AI 分析。
- 第一阶段不下载群组里的全部媒体。
- 不强制老用户切换到新的文件夹布局。
- 不引入云存储依赖。

## 用户问题

被追踪用户经常不会按 Telegram 的“回复”按钮，而是直接在群里输入一句话。这样消息语义就会丢失。

例子：

```text
03:05 群友: F哥, 工业富联这里还能买吗？
03:06 F哥: 破十日线了
```

现有数据库会保存 03:06 这条 tracked-user 消息，但不一定保存 03:05 的问题。以后看到“破十日线了”，就无法可靠判断它说的是哪只股票。

全量归档通过保存同一群组或 Topic 的完整时间线来解决这个问题。

## 产品行为

### 现有 watcher

当前 watcher 继续负责：

- 指定用户追踪；
- tracked 消息媒体；
- 报告；
- 控制群推送；
- realtime 模式；
- reply snapshot；
- 控制命令。

### 全量归档

全量归档新增一个被动采集层：

- 监听同一个 Telegram 群组消息流；
- 监听该群组的 message edit，并用 `(chat_id, message_id)` 幂等更新 archive-only 行；
- 保存配置范围内的消息；
- 创建到现有 tracked-message 记录的连接；
- 为未来本地分析或 Codex 手动查库提供上下文；
- 不发送通知。

## 配置概念

第一版优先支持一个源群组：

```toml
[full_archive]
enabled = false
root_dir = "data/full_archive"
source_chat_id = -1001234567890
capture_scope = "whole_group"  # "whole_group" | "topics"
topic_ids = []                 # enabled = true 且 capture_scope = "topics" 时必填
shard_policy = "monthly"
max_messages_per_shard = 500000
max_shard_size_mb = 1024
backfill_limit_messages = 10000
```

`source_chat_id` 优先填写当前 `targets` 中的一个 `target_chat_id`。如果它不是任何 target，归档仍然是本地、可删除的，但它不会自然产生 tracked_ref，也很可能无法恢复 tracked 用户短消息上下文；因此第一阶段用 `doctor` 和 GUI warning 提醒，而不是在 parser 阶段硬失败。

未来多群组可以演进为下面的结构，但这不是第一阶段配置格式，当前 parser 不接受：

```toml
[[full_archive.sources]]
name = "main-invest"
source_chat_id = -1001234567890
capture_scope = "topics"
topic_ids = [12345, 12346]
```

## Topic 发现体验

理想流程：

1. 用户输入群组 ID。
2. 应用检查这个群组是否为 forum。
3. 如果是 forum，应用尝试列出 Topic。
4. 用户选择一个或多个 Topic。
5. 如果 Topic 列表获取失败，应用允许手动输入 Topic ID，或者退回整群归档。
6. Topic ID `1` 是 General 的保留 ID，不能写入 `topic_ids` 做过滤；需要 General 时使用整群归档。

兜底流程：

1. 用户输入群组 ID。
2. 用户不开 Topic 过滤。
3. 应用归档整个群组。

这个兜底可以接受，因为数据只保存在本地，而且全量归档目录可以独立删除。

## 存储行为

### 避免重复

如果一条全量归档消息同时也是 tracked-user 消息：

- tracked DB 保留完整 tracked 行和媒体引用；
- full archive 只保存带 `tracked_ref_*` 字段的轻量引用行；
- link table 记录两者关系。

Telegram 消息身份始终使用：

```text
(chat_id, message_id)
```

### 删除边界

删除 full archive 文件不能破坏：

- tracked DB；
- tracked media；
- reports；
- control-chat pushes。

如果 tracked DB 被删除，全量库里的 tracked link 可能失效。这可以接受，link 校验可以把它标记为 missing。

## 数据保留策略

第一版不自动删除全量归档数据，也不支持 `[full_archive] retention_days`。原因是全量归档的删除语义比报告文件更敏感：自动删除某个 shard 会直接影响未来上下文还原的完整性，必须先有明确的 UI、status 提示和备份预期。

手动删除仍是第一版主要的安全阀：

- 删除某个月的 shard 文件；
- 删除某个 group 文件夹；
- 删除整个 `data/full_archive` 文件夹。

如果只删除某个月 shard 或某个 group 文件夹，`manifest.sqlite3` 里会暂时保留对应记录，`archive-status` 会继续提示缺失。确认这些文件确实是手动清理后，再运行 `archive-repair --prune-missing-shards --apply` 清掉缺失文件对应的 manifest 行。这个命令只删 manifest 记录，不会删除任何 shard 文件、媒体文件或 tracked DB。

如果删除的是整个 `data/full_archive` 文件夹，这等同于用户主动重置全量归档。之后 `archive-status` 应显示 empty，`archive-context` 应只读返回空上下文且不能重新创建目录；下一次 live capture 或 `archive-backfill --apply` 写入时，再从新的空状态重新创建 manifest 和 shard。这个恢复过程不能移动、重建或清空 tracked DB。

如果未来加入自动保留策略，需要作为单独设计进入：先由 `archive-status` 标明会影响哪些 shard，再由显式命令或 GUI 操作执行，不能作为隐藏后台清理。

## 隐私边界

这个功能会把存储范围从“少数被追踪用户”扩大到“更大范围的群组上下文”。因此：

- 默认必须关闭；
- 配置和文档必须明确说明范围变化；
- 数据只保存在本地；
- 第一阶段不上传 AI；
- 第一阶段不下载全量媒体；
- 不引入自动云依赖；
- 不打印 secrets。

## UX 风险与缓解

| 风险 | 缓解 |
|---|---|
| 用户不知道 Topic ID | 增加 Topic 列表命令；失败时允许整群归档 |
| Topic 列表获取失败 | 手动输入 Topic ID；给出清晰提示 |
| 归档越来越大 | 按月、条数、大小分片；文件夹可删除 |
| tracked 消息重复存储 | 用 link table 连接，不重复保存正文和媒体 |
| 旧 DB 迁移出错 | 不改 tracked schema，新增独立数据库族 |
| 未来 AI 看到过多数据 | 第一阶段不加 AI；以后再加明确范围和窗口 |

## 未决设计点

1. GUI 是否要做 Topic 选择器。当前第一版已经采用配置 + `list-topics` CLI 辅助，GUI 只提供手动输入 Topic ID。
2. 是否展示或过滤 Telegram service messages。当前实现已经按 `message_kind = "service"` 写入时间线，保证归档完整性；真实群组中的价值和噪音比例仍需 QA 决定后续 UI 是否默认隐藏。
3. 是否保存 sender profile snapshot，还是只保存 sender ID。当前实现只保存 sender ID，避免额外 API 调用。
4. backfill 是否允许和 live capture 并行运行。当前第一版采用独立 `archive-backfill` 命令，由用户显式执行。

## 建议

第一版采用 config-first：

- 先保证整群归档可用；
- 支持手动 `topic_ids`；
- 提供 `list-topics` CLI 作为辅助发现能力，失败时回退到手动 Topic ID 或整群归档；
- GUI Topic 选择器作为后续 UX 层。
