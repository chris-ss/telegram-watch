# 全量消息归档总览

## 一句话目标

在不改变现有“只追踪指定用户”的推送逻辑前提下，新增一个可选的全量消息归档层，静默保存指定 Telegram 群组或 Topic 的完整消息时间线，让以后分析时能补回上下文。

## 为什么要做

当前产品回答的是：“这几个被追踪的人说了什么？”

现在缺的是第二层能力：“他们说这句话时，前后到底发生了什么？”

在投资群里，被追踪的人可能只说一句“这个可以买”“破十日线了”“等回踩”。如果他没有点 Telegram 的“回复”按钮，现有数据库只能知道他说了这句话，却不知道他指的是哪只股票、回答的是谁的问题、上下文是否已经变了。

全量消息归档要解决的就是这个问题：把同一个群组或同一个 Topic 的完整时间线保存下来。未来无论是人工查库，还是交给 Codex 分析，都可以看到这条被追踪消息前后发生了什么。

## 核心决策

- 现有 tracked-user SQLite 数据库和推送行为保持不变。
- 新增独立的全量归档数据库族，可以单独开启、关闭、删除。
- 第一版只支持配置一个源群组；如果该群组是 Telegram forum，可以进一步限制到指定 Topic。
- 第一阶段不加入 AI、OpenAI、ChatGPT、embedding、总结、自动清理。
- 第一阶段不下载全量媒体，但会保留足够的媒体元信息，方便以后补媒体归档。
- 被追踪用户的消息不在全量库里重复存正文和媒体；全量库只保存最小引用行，并通过 link table 连回原 tracked DB。
- 大规模归档用“文件夹 + 多个 SQLite 分片”管理，而不是长期只堆在一个 SQLite 文件里。

## 产品形态

用户继续按原方式使用 telegram-watch：

1. 当前 watcher 继续只追踪配置里的用户。
2. 当前 watcher 继续把被追踪用户的消息和媒体写入现有数据库。
3. 当前 watcher 继续发送报告和控制群推送。
4. 如果开启全量归档，程序会在后台额外保存指定群组或指定 Topic 的所有消息记录。
5. 全量归档本身不推送、不提醒、不改变控制群体验。

全量归档是“上下文数据层”，不是新的通知系统。

## 范围

### 第一阶段包含

- 一个全量归档源群组。
- 源群组最好是当前 `targets` 中的一个 `target_chat_id`。否则归档仍可运行，但它可能无法服务“恢复 tracked 消息上下文”这个核心目标，`doctor` 和 GUI 会给出 warning。
- 可选 forum topic 模式：
  - 不开 Topic 过滤时，归档整个群组；
  - 开 Topic 过滤时，只归档配置的 Topic。
- 新的 SQLite 归档文件夹和分片元数据。
- 连接全量消息和 tracked 消息的 link table。
- 有限制的历史回填 backfill。
- 新消息的实时静默采集。
- `doctor` 检查归档目录是否可写、分片是否可创建。
- `archive-status` 只读检查归档 manifest、分片文件和 link 统计，方便删除、恢复和排障。
- `archive-context` 离线查询某条 tracked message 前后的全量上下文，并把 `tracked_ref` 的正文从 tracked DB 补回来。

### 第一阶段不包含

- AI 分析。
- 全量媒体下载和媒体去重。
- 多群组全量归档。
- 云同步。
- 多设备同步。
- 由 GPT 自动删除消息。
- 修改现有报告或推送行为。

## 调研结论

- Telegram forum topic 是 MTProto 里的正式能力。消息头里有 `reply_to_top_id` 等 thread/topic 字段。Telethon raw API 在不同版本里可能暴露为 `functions.channels.GetForumTopicsRequest` 或 `functions.messages.GetForumTopicsRequest`，`list-topics` 需要兼容两种位置。
- Telethon 支持 raw MTProto request，因此即使高层 API 不完善，也应该可以通过 Telethon 调用 topic 列表接口。
- SQLite 理论上可以存远超几十万、几百万条消息。这里建议分片，不是因为 SQLite 扛不住，而是为了删除、备份、恢复、人工检查和 GUI 查询更可控。
- 本机 Python SQLite 构建显示 SQLite `3.51.0`，`MAX_PAGE_COUNT=1073741823`。这个上限远高于本项目预期，不是硬性瓶颈。

## 推荐架构

使用两套逻辑存储：

```text
data/
  tracked/
    tgwatch.sqlite3              # 现有 tracked-user DB，保持迁移兼容
    media/                       # 现有 tracked 媒体

  full_archive/
    manifest.sqlite3             # 分片注册表和全局元数据
    shards/
      group_-1001234567890/
        2026-05.sqlite3          # 月度分片，或达到阈值后创建序号分片
        2026-06.sqlite3
    exports/
```

为了兼容老用户，现有路径可以继续保持 `data/tgwatch.sqlite3`。上面的新布局是未来推荐结构，第一版不应该强制迁移旧用户目录。

## 用普通话解释数据模型

数据库里会有三类记录：

- **全量归档消息**：在指定群组或 Topic 中观察到的每一条消息。
- **Tracked 消息**：现有数据库里被追踪用户的消息，包含现有媒体和报告字段。
- **Link 记录**：告诉系统“全量时间线里的这条消息，其实就是 tracked DB 里的那条消息”。

如果同一条 Telegram 消息已经存在于 tracked DB，全量库不重复保存它的正文和媒体。全量库只保存时间线所需的最小引用信息，然后通过 link table 回连到 tracked DB。

## 为什么要拆成两个数据库

两个数据库能把产品边界划清楚：

- 用户可以删除全量归档，不影响原来的 watcher。
- 用户可以关闭全量归档，不影响报告和推送。
- tracked DB 保持小而稳定。
- 未来 AI 实验可以读很大的上下文库，不会影响核心通知数据库。

查询时，SQLite 可以通过 `ATTACH DATABASE` 把全量库和 tracked DB 连起来。

## 分片建议

默认分片策略：

- 按群组 + 月份创建分片：`YYYY-MM.sqlite3`。
- 如果单个分片提前达到下面任一阈值，也提前切新分片：
  - 500,000 条消息；
  - 1 GB 文件大小。

原因：

- SQLite 可以处理更大的文件，但 50 万条 / 1GB 的分片更容易复制、删除、检查、`VACUUM` 和恢复。
- 应用以后可以很自然地提供“删除 2026 年 5 月归档”这种操作。
- 上下文查询通常只查某条 tracked 消息前后几分钟或几小时，跨月查询很少见。

## Topic 策略

第一阶段支持两条路线：

- 简单路线：归档整个配置群组。
- Topic 路线：如果群组是 forum，并且配置了 Topic ID，则只归档这些 Topic。
- Topic ID `1` 代表 General，第一阶段不把它当作可过滤 Topic；如果需要 General 上下文，请使用整群归档。

为了直接解决 tracked 用户短消息的上下文缺失，`source_chat_id` 应优先配置为已有 target 群组。第一阶段不强制禁止归档非 target 群组，因为用户可能已经有历史 tracked DB 或临时调试需求；但这种配置会降低 link/context 的可用性，所以 `doctor` 和 GUI 必须提示用户确认。

Topic 发现应该作为辅助能力，而不是第一版的强依赖：

- 优先尝试通过 Telethon `functions.channels.GetForumTopicsRequest` 列出 Topic；如果当前安装版本只暴露 `functions.messages.GetForumTopicsRequest`，则自动回退。
- 如果因为权限、API 差异或限流失败，则允许用户手动填 Topic ID。
- 如果手动填 Topic 成本太高，可以先用整群归档。

## 迁移原则

不要把现有 tracked DB 强行改造成新的全量库。

正确做法是：

1. 保持现有 tracked schema 可用。
2. 增加新的 full archive 配置项。
3. 初始化新的 archive 数据库。
4. 可选地从 Telegram 历史消息回填上下文。
5. 当归档消息与 tracked 消息 `(chat_id, message_id)` 相同时，创建 link 记录。

如果全量归档关闭，不应该创建任何新的数据库文件。

## CEO 能看懂的验收标准

- 原来的 watcher 还能完全照常工作。
- 用户可以给一个群组开启全量归档。
- 新消息会被保存在本地独立归档里。
- 被追踪用户的消息能和前后群聊消息连起来。
- 用户可以输入 tracked message 的 chat/message ID，导出它前后几分钟的上下文。
- 用户可以删除归档库，不会破坏原来的追踪库。
- 用户删除整个归档文件夹后，状态检查会把它当作空归档；下一次开启 live capture 或执行 `archive-backfill --apply` 时会重新创建归档文件。
- 用户可以运行状态检查，知道归档库是否存在、分片是否缺失、link 是否已经建立。
- 第一版没有 AI 成本、没有模型风险、没有推送行为变化。

## 交付证明分级

为了避免把“离线测试通过”误说成“真实端到端可交付”，本功能按三层证明交付状态：

- **离线可合并**：`pytest tests/`、`doctor`、`archive-status`、默认关闭不建库等本地检查通过；这只能证明代码路径和数据库规则基本可信。
- **本机可验收**：在真实 `config.toml` 上确认 full archive 开启、关闭、整删归档目录、`archive-context` 查询和 `archive-repair` 诊断都符合预期；仍不能替代 Telegram live 验证。
- **真实端到端可交付**：使用 `archive-qa-init` 生成 gitignored QA 草稿，并完成真实 Telegram user account 的 whole-group live capture、必要的 Topic capture、backfill 幂等和 `archive-context` 验证。没有这份脱敏 QA 记录时，只能说“离线测试通过”，不能说 full archive 已经端到端可交付。

CR 只能按已经被证据证明的最高层级下结论：没有真实 Telegram QA 记录时，CR 结论必须写成“离线通过，待真实 QA”或“本机可验收，待真实 QA”，不能写“真实端到端可交付”。CR 如果发现文档、实现、测试或验证证据不一致，必须先修复，或把剩余风险明确写进测试文档和 QA 记录模板。

## 文档地图

- [设计文档](DESIGN.md)：产品行为、用户流程、取舍。
- [架构文档](ARCHITECTURE.md)：存储布局、schema、link 策略、分片管理、运行流程。
- [开发文档](DEVELOPMENT.md)：实施步骤、模块划分、配置改动、迁移规则、发布顺序。
- [测试文档](TESTING.md)：单元测试、集成测试、迁移测试、真实 Telegram 验证、回归检查。
- [真实 Telegram QA 记录模板](REAL_TELEGRAM_QA_TEMPLATE.md)：人工真实验证时通过 `archive-qa-init` 生成脱敏草稿，用来证明端到端行为和记录未验证风险。
- [非真实 TG CR 审计](CR_AUDIT.md)：在真实 Telegram QA 之外，逐项记录文档、实现、测试和剩余风险的当前证据。

## 外部参考

- Telegram Threads API: https://core.telegram.org/api/threads
- Telegram `messageReplyHeader`: https://core.telegram.org/constructor/messageReplyHeader
- Telethon `channels.GetForumTopicsRequest` / `messages.GetForumTopicsRequest`：以当前安装的 Telethon 版本为准。
- Telethon client API: https://docs.telethon.dev/en/stable/modules/client.html
- SQLite limits: https://www.sqlite.org/limits.html
- SQLite ATTACH DATABASE: https://www.sqlite.org/lang_attach.html
- SQLite WAL mode: https://www.sqlite.org/wal.html
