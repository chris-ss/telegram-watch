# 全量消息归档开发文档

## 实施原则

- 保持现有 tracked-user 行为不变。
- 全量归档默认关闭，必须显式开启。
- 归档存储和 tracked 存储分离。
- 保证迁移安全：旧的 `tgwatch.sqlite3` 继续可用。
- 先补测试，再做大范围真实 Telegram 验证。
- 不加入 AI 依赖。

## 任务拆分

### 任务 1：配置模型

新增配置字段：

```toml
[full_archive]
enabled = false
root_dir = "data/full_archive"
source_chat_id = -1001234567890
capture_scope = "whole_group"
topic_ids = []
shard_policy = "monthly"
max_messages_per_shard = 500000
max_shard_size_mb = 1024
backfill_limit_messages = 10000
```

校验规则：

- `enabled` 必须是 bool。
- 开启后 `source_chat_id` 必填。
- `capture_scope` 只能是 `whole_group` 或 `topics`。
- `enabled = true` 且 `capture_scope = "topics"` 时，`topic_ids` 必须非空；关闭状态允许用户先保存 Topic 模式草稿，但不会创建 archive 文件或连接 Telegram。
- `topic_ids` 只能保存大于 `1` 的 Telegram forum topic root ID；`1` 代表 General，第一阶段归为 `NULL`，需要通过 `whole_group` 归档覆盖。
- 分片阈值必须为正数。
- `backfill_limit_messages = 0` 表示默认不做历史回填；此时 `archive-backfill` 在未显式传 `--limit` 时应成功 no-op，不连接 Telegram、不创建 archive 文件。
- `doctor` 必须检查 `root_dir`、`root_dir/shards/` 和 manifest DB 是否可创建、可写；目录检查不能只 `mkdir` 成功就算通过，还要写入并删除一个本地探针文件。

### 任务 2：归档存储模块

新增 `telegram_watch/full_archive_storage.py`。

职责：

- 初始化 manifest DB；
- 初始化 shard DB；
- 选择 active shard；
- 按群组 + 月份 + 序号选择 shard；
- 按月份、条数、大小轮转 shard；大小判断必须计算 `.sqlite3`、`-wal`、`-shm` 的合计磁盘占用，避免 WAL 文件很大但主 DB 文件很小时不切分；
- 新写入的 manifest shard path 必须保存为相对 `root_dir` 的路径，同时读取层兼容旧 absolute path；
- upsert archive message；
- 创建 tracked link；
- 查询上下文窗口。

不要改动现有 `telegram_watch/storage.py`，除非确实需要提取很小的共享 helper。

### 任务 3：归档消息归一化

新增 dataclass：

```python
@dataclass(frozen=True)
class ArchiveMedia:
    media_index: int
    media_kind: str
    mime_type: str | None
    file_size: int | None
    file_name: str | None

@dataclass(frozen=True)
class ArchiveMessage:
    chat_id: int
    message_id: int
    topic_id: int | None
    sender_id: int | None
    date: datetime
    text: str | None
    raw_text: str | None
    message_kind: str
    reply_to_msg_id: int | None
    reply_to_top_id: int | None
    is_forum_topic_link: bool
    has_media: bool
    media: tuple[ArchiveMedia, ...] = ()
```

归一化阶段不下载媒体，只读取 Telegram message 已暴露的轻量媒体元数据。

异常字段处理规则：

- `message_id`、`date` 或最终 `chat_id` 缺失/不可解析时返回 `None`，由 live/backfill 调用方跳过；
- 如果 message 上的 `chat_id` 为 `None`，可以使用调用方传入的 `chat_id_default`；
- `sender_id`、reply/thread id、媒体大小等可选字段不可解析时保存为 `None`，不能抛异常中断 backfill；
- `archive-backfill` 对这类无稳定身份的消息计入 `skipped_invalid`，不创建 archive 文件以外的副作用。

### 任务 4：实时采集 handler

在 `_TargetHandler` 旁边新增 full archive handler。

规则：

- handler 检查 `full_archive.enabled`；
- handler 只接收 `source_chat_id`；
- handler 同时注册 `events.NewMessage` 和 `events.MessageEdited`；
- 如果配置了 Topic，则应用 Topic 过滤；
- handler 只写本地 archive DB；
- handler 永不发送控制群消息。
- handler 异常只记录日志，不影响 `_TargetHandler`。
- daemon 启动时必须执行一次只读 archive health preflight；如果当前 archive degraded，daemon 仍启动现有 tracked-user watcher，但本次运行不注册 full archive live handler，也不做 tracked-user post-persist relink，避免继续写入损坏或不一致的归档层。
- `_TargetHandler` 成功写入 tracked DB 后，如果 full archive 也覆盖同一条消息，必须补一次 archive upsert，将该行转为 `tracked_ref`，避免因为 handler 执行顺序导致 tracked 消息正文长期重复保存在 archive DB。
- `_TargetHandler` 里的 relink 必须后台调度，不能让 realtime queue / 后续推送链路等待 archive SQLite 写入；handler 必须持有 pending relink task 的强引用并在完成后移除；失败只能记录 warning，不能回滚 tracked DB 写入。
- daemon 关闭时必须对 pending relink task 做短时间有界等待，尽量在退出前完成 tracked_ref 去重；超时或 task 已被取消时只能记录 warning，不能无限阻塞退出，也不能影响 tracked DB 已经完成的写入。
- edited-message 走同一条 upsert 路径，普通 archive row 更新原行；`tracked_ref` 继续只保留引用，不改变 tracked DB 行为。
- runner 级测试必须覆盖 edited-message + `tracked_ref` 组合：同一消息已经是 `tracked_ref` 后，后续 edited event 不能把 archive row 改回保存正文或 archive 侧媒体元数据，也不能重复增加 manifest message count。
- runner 级测试必须覆盖真实 handler 顺序：`_FullArchiveHandler.handle()` 先把 tracked 用户消息写成普通 archive row，随后 `_TargetHandler.handle()` 写入 tracked DB 并 relink，同一 archive row 最终必须变为 `tracked_ref`，且不保留重复正文或 archive 侧媒体元数据。
- runner 级测试也必须覆盖反向顺序：`_TargetHandler.handle()` 先写 tracked DB 并创建 `tracked_ref`，随后 `_FullArchiveHandler.handle()` 到达同一消息时必须保持 `tracked_ref` 幂等，不得重新写入重复正文、媒体元数据或增加 manifest message count。
- Topic 模式下，tracked-user 路径的 post-persist relink 也必须遵守 full archive scope：tracked DB 仍照常保存所有 tracked 用户消息，但未配置 Topic 或 General/未知 Topic 的 tracked 消息不能在 full archive 里创建 `tracked_ref`，避免绕过 `_FullArchiveHandler` 的 Topic 过滤。
- 如果 startup preflight 判定 archive degraded，tracked-user 路径的 post-persist relink 必须禁用；这只是禁用 full archive 旁路，不能影响 tracked DB、报告、推送或 realtime。

现有 tracked handler 继续负责 tracked-user 逻辑。

### 任务 5：Link 创建

archive writer 持久化消息时：

1. 检查 tracked DB 是否存在同一个 `(chat_id, message_id)`。
2. 如果存在：
   - 设置 `payload_mode = 'tracked_ref'`；
   - archive 行里重复的 tracked payload 字段保持为空；
   - 插入 `archive_tracked_links`。
3. 如果不存在：
   - 按普通 archive row 保存。
4. 如果现有 archive row 已经是 `tracked_ref`，后续同一消息即使暂时无法解析当前 tracked DB，也必须保留 `tracked_ref` 和既有 link 元数据；不能降级成普通 archive row，不能重新保存 tracked 正文或 archive 侧媒体元数据。
5. 新 archive row 的 manifest `message_count` / `file_size_bytes` 更新必须先于 manifest `tracked_db_links` 登记；如果 shard row 已写入但 manifest 写入计数失败，不能再留下 active tracked DB link，避免 `archive-status` 把一次未完成写入误判为健康连接。

第一版推荐：

- 不重复保存 tracked message text；
- 不重复保存 tracked message media；
- archive 行设置 `payload_mode = 'tracked_ref'`；
- archive 行只保留时间线和引用元数据；
- 新写入的 archive row 和 manifest link 中的 tracked DB path 保存为相对 `root_dir` 的路径，读取时兼容旧 absolute path；
- text、media、reply snapshot 都从 tracked DB 读取；
- 如果 archive row 先保存过普通媒体元数据，后续 relink 成 `tracked_ref` 时删除 archive 侧 `archive_media` 行。

### 任务 6：Backfill 命令

新增 CLI 命令或参数：

```bash
python -m tgwatch archive-backfill --config config.toml --limit 10000 --apply
```

采用独立 `archive-backfill` 命令，避免 daemon 启动时行为不可预测。默认 dry-run，只扫描和输出统计；显式 `--dry-run` 与省略模式参数等价，便于 QA runbook 写出明确意图；只有显式传 `--apply` 才写入 archive DB。`--apply` 和 `--dry-run` 互斥，不能同时出现。`--limit` 省略时使用 `full_archive.backfill_limit_messages`。如果有效 limit 为 `0`，命令成功返回空统计，不连接 Telegram，也不创建 archive 文件；用户仍可通过显式 `--limit N` 临时执行回填。

CLI help 必须把这个安全边界讲清楚：`--limit` 接受非负整数，`0` 表示 no-op。真实 QA 经常先把 `backfill_limit_messages = 0` 作为安全默认值，如果 help 只写 maximum messages，容易让用户误以为 `0` 是非法值或不知道它不会连接 Telegram。

真实 Telegram 回填必须显式处理限流风险：超过 1,000 条的 backfill 给 `iter_messages` 传 `wait_time=1.0`；如果迭代中遇到 `FloodWaitError`，睡眠 `seconds + 1` 后用最后已处理的 `message_id` 作为 `offset_id` 继续拉取，避免重复扫描或丢失已完成进度。即使原本是小批量 backfill，第一次 FloodWait 后，同一次命令的后续请求也要切换到 `wait_time=1.0`，避免恢复后立刻再次触发限流。

`archive-backfill --apply` 的输出统计必须区分新增、补 link 和幂等更新：首次写入普通 archive row 时计入 archived rows；首次写入 tracked_ref 或把已有 archive-only row 升级为 tracked_ref 时计入 tracked links；如果同一条 Telegram 消息已经存在且没有新增 link，只能计入 updated rows，不能让重复 apply 看起来像新增了 row 或 link。

Topic 模式下，`archive-backfill` 必须复用 live capture 的同一套 scope 判断：配置 Topic 内消息计入 matched，其他 Topic、General 或未知 Topic 计入 `skipped_scope`，不能写入 archive shard。这样历史回填和 daemon 实时监听不会产生两套不同的 Topic 时间线。

`archive-backfill --apply` 写入前必须做一次只读 archive health preflight：如果当前 `archive-status` 会返回 degraded（例如缺失 shard、link 损坏、schema 缺失、tracked_ref 残留重复 payload），命令必须拒绝连接 Telegram 和写入 archive，提示先运行 `archive-status` / `archive-repair`。manifest 不存在的 empty 状态可以继续，因为这是第一次写入或用户整删归档后的正常恢复路径。有效 limit 为 `0` 时仍按 no-op 处理，不执行 health preflight；因为该命令不会连接 Telegram，也不会写 archive，不能被旧归档损坏状态拦截。

如果用户删除了整个 `full_archive.root_dir`，后续 live capture 或 `archive-backfill --apply` 必须按全新归档处理：重新创建 manifest 和目标 shard，只统计本次重新写入的消息，不能依赖已经被删除的旧 manifest，也不能影响 tracked DB。重新写入时仍必须执行 tracked 去重：如果当前 tracked DB 已有同一 `(chat_id, message_id)`，新 archive row 必须直接保存为 `tracked_ref`，不能因为 archive root 是新建的就重复保存 tracked 正文或 archive 侧媒体元数据。只读命令在此之前仍必须保持只读，不因查询或 status 自动建目录。

### 任务 7：Topic 发现 helper

已提供命令：

```bash
python -m tgwatch list-topics --config config.toml --chat -1001234567890
```

预期输出：

```text
Topic ID    Top Message  Archive Use    Flags        Title
1           1            whole_group    -            General
12345       12345        topic_ids      pinned       FLT
12346       12346        topic_ids      -            雅克科技
```

实现：

- 使用 Telethon raw function wrapper 调 forum topics request：优先 `functions.channels.GetForumTopicsRequest(channel=...)`，当前安装版本若只暴露 `functions.messages.GetForumTopicsRequest(peer=...)` 则回退；
- 输出必须区分普通 Topic 和 General：`topic_id > 1` 的行显示可用于 `full_archive.topic_ids`，`topic_id = 1` 显示应使用 `capture_scope = "whole_group"`，不能误导用户把 `1` 写入 `topic_ids`；
- 权限、entity 解析失败、API 缺失或签名差异要优雅失败，统一给出 fallback 说明，不能把 Python traceback 当成用户界面；
- 打印手动填写 Topic ID 或改用整群归档的 fallback 说明。

Topic 发现只是辅助能力，不是归档启动的强依赖。如果 API 权限、Telegram 版本或群组状态导致无法列出 Topic，用户仍可手动填写 Topic ID，或者把 `capture_scope` 改回 `whole_group` 先做整群归档。

### 任务 7.5：真实 QA 记录草稿

提供本地命令：

```bash
python -m tgwatch archive-qa-init --config config.toml
```

该命令从包内模板读取内容，并把草稿写到 `reporting.reports_dir/full_archive_qa/REAL_TELEGRAM_QA_<date>.md`。包内模板必须随 `telegram_watch` package data 一起发布，源码树中的 `docs/full-message-archive/REAL_TELEGRAM_QA_TEMPLATE.md` 是给开发者阅读和维护的同源副本。`reports/` 是 gitignored，本地 QA 记录应放在这里或其他 gitignored 路径，不应把填好的真实 Telegram 记录提交到 `docs/`。命令不能读取或打印 Telegram secret、session、手机号、私密群名；如果目标文件已存在，默认拒绝覆盖，只有显式 `--force` 才覆盖；如果用户显式把 `--output` 指向源码树的 `docs/` 目录，命令必须拒绝写入，避免真实 QA 记录误进入可提交文档区。

维护规则：

- `docs/full-message-archive/REAL_TELEGRAM_QA_TEMPLATE.md` 和 `telegram_watch/templates/REAL_TELEGRAM_QA_TEMPLATE.md` 必须逐字同步；
- 开发者可以先改 `docs/` 副本，但提交前必须同步到 package data 副本；
- 测试必须直接读取 package data 模板本体，并覆盖它与 docs 模板内容一致，避免 fallback 掩盖发布包漏带模板的问题。
- `archive-qa-init` 运行时只允许读取 package data 模板；如果发布包漏带模板，应明确报错，不能回退读取源码树 `docs/` 副本。
- 如果当前 config 的 `[full_archive] enabled = false`，`archive-qa-init` 仍可以生成草稿，但必须打印 warning，明确这份配置不能完成真实 Telegram QA；真实 QA 的初始 `archive-status` 如果是 `disabled`，必须先启用 full archive 后再继续。
- 生成的草稿必须在文件内记录 `archive-qa-init` 执行时 `full_archive.enabled` 的值。终端 warning 不够，因为真实 QA 记录可能稍后才被阅读；文件本身要能证明它是不是在 disabled 配置下生成的。
- 生成的草稿还应自动记录当前源码修订：如果命令运行在 git checkout 中，写入短 commit SHA，并在工作区有未提交改动时标记 `dirty`；如果不是 git checkout 或无法读取，则写入 `unknown`。这个字段不能展开 changed file 列表，避免把本机路径或敏感文件名带入 QA 记录。
- 生成的草稿还应自动记录 Python 版本和 Telethon 版本；Telethon 版本无法从安装元数据读取时写 `unknown`。这些环境字段不包含 secrets，可以直接写入 QA 草稿，减少真实验证时手工漏填。
- 生成的草稿还应自动记录不泄露群号的 full archive 配置摘要：`capture_scope`、`topic_ids` 数量、`backfill_limit_messages`，以及 `source_chat_id` 是否已经配置且是否匹配当前 targets。不要自动写入真实 `source_chat_id` 或 Topic ID 列表，避免把私密群组范围带进可分享的 QA 记录。

### 任务 8：Archive status 只读诊断

新增命令：

```bash
python -m tgwatch archive-status --config config.toml
```

输出内容：

- full archive 是否启用；
- manifest 路径是否存在；
- manifest 不存在且 `root_dir/shards/` 下没有 shard DB 时状态为 empty，命令成功；这是尚未写入归档，不是 degraded；
- manifest 不存在但 `root_dir/shards/` 下仍有 shard DB 时状态必须是 degraded，不能当作 empty；这是 manifest 丢失或手动误删后的孤儿归档数据，继续写入可能让新 manifest 与旧 shard 计数不一致；
- manifest 存在但 `root_dir/shards/` 下出现未登记在 manifest 的 shard DB 时状态必须是 degraded；这是隐藏归档数据或人工复制残留，不能被 status 忽略；
- shard 数量、缺失 shard 数量；
- manifest 记录的消息数；
- 实际可读 shard 中的消息数；
- `archive` / `tracked_ref` / link / archive media metadata 行统计；
- 只要 manifest/shard 已有归档消息但没有任何 active tracked DB link，就标记 degraded；这个判断不依赖当前 config 的 tracked DB 路径；
- `tracked_ref` 行必须包含完整 link 元数据；缺少 `tracked_db_path`、`tracked_message_chat_id` 或 `tracked_message_id` 时，status 必须显式报告 incomplete tracked_ref metadata，repair 不能猜测补齐；
- `tracked_ref` 行数和 link 行数相同也不能直接判定健康；必须继续比对 link 内容，避免 stale/wrong link 指向错误 tracked DB 或 message；
- `tracked_ref` 行如果残留 archive 侧 `text` / `raw_text`，status 必须标记 degraded，repair 可以清空这些重复正文；
- `tracked_ref` 行如果残留 archive 侧 `archive_media` 元数据，status 必须标记 degraded，repair 可以删除这些派生元数据；
- 当前配置的 tracked DB 是否已登记为 active link，以及是否可只读打开并包含 `archive-context` 需要的 tracked `messages` schema：`chat_id`、`message_id`、`date`、`text`、`replied_text`；
- manifest 自身缺少可增量创建的 schema 表（例如 `tracked_db_links`）时必须标记 degraded，不能静默当成 0 个 link；
- degraded/error 摘要。
- CLI 退出码必须以整体 health 为准：只要状态是 degraded 就返回非零，不能只检查当前实现里的 `errors` 列表。这样未来新增 missing shard、schema、index 或 link health 条件时，不会出现屏幕显示 degraded 但命令退出码为 0 的矛盾。
- storage 层的 `ArchiveStatusReport.degraded` 必须从结构化计数和 errors 一起推导；`errors` 是给人看的诊断摘要，不应成为唯一的 health source。

实现要求：

- 关闭时不创建任何 archive 文件；
- 只读打开 manifest 和 shard；
- 只读检查当前 tracked DB；如果 DB 不存在、只是空 SQLite 文件，或缺少上下文查询需要的 tracked `messages` 列，只报告 unreadable/degraded，不创建空 tracked DB，不打印本机路径；
- 某个 shard 损坏或缺失时继续检查其他 shard；
- 不连接 Telegram，不触发 backfill，不修改 manifest。

### 任务 8.5：Archive repair 显式修复

新增命令：

```bash
python -m tgwatch archive-repair --config config.toml --dry-run
python -m tgwatch archive-repair --config config.toml --apply
```

实现要求：

- 默认 dry-run，只报告会修复的项目；
- `--apply` 才写入 manifest 或 shard；
- manifest 不存在且没有孤儿 shard 时成功 no-op，且不创建 archive root；
- manifest 不存在但存在孤儿 shard 时报告不可自动修复并返回非零；第一阶段不猜测重建 manifest，用户应先恢复 manifest、备份后删除整个 `root_dir`，或保留数据等待后续专门恢复工具；
- manifest 存在但有未登记 shard 时报告不可自动修复并返回非零；第一阶段不猜测把这些文件注册回 manifest，也不删除文件；
- 可修复项包括缺失索引、可增量补齐的 schema 表、manifest message count/file size、可由完整 `tracked_ref` 元数据重建的 `archive_tracked_links`、以及 `tracked_ref` 下残留的 archive 侧正文和 `archive_media` 派生元数据；file size 同样按 `.sqlite3` + `-wal` + `-shm` 计算；
- 可增量 schema 修复同时覆盖 manifest 和 shard：manifest 缺少 `tracked_db_links` 时，dry-run 报告，`--apply` 创建空表；这不等于补 link 数据，只是恢复 schema；
- `archive_tracked_links` 修复只使用 shard 内已有字段，不连接 Telegram，不猜测缺失的 tracked DB 或 message id；
- 如果 shard 里存在 incomplete tracked_ref metadata，`archive-repair` 必须报告不可修复错误并返回非零，不能用成功 no-op 掩盖仍然 degraded 的状态；
- 缺失 shard 文件默认报告 skipped 并返回非零，不自动创建空 shard；
- 用户确认缺失 shard 是手动清理结果后，可用 `--prune-missing-shards --apply` 删除 stale manifest 行；
- prune 只删除 manifest 记录，不删除 shard 文件、tracked DB 或媒体文件。
- CLI help 必须说明 `--prune-missing-shards` 在默认 dry-run 模式下只报告会清理的 manifest 行，只有同时传 `--apply` 才会写入 manifest，避免用户误以为单独加该参数会立即删除记录。

### 任务 9：Doctor 检查

扩展 `doctor`：

- archive root dir 可创建、可写（通过本地写入探针验证）；
- manifest DB 可创建；
- shard dir 可创建、可写（通过本地写入探针验证）；这里只验证 `root_dir/shards/` 目录本身，不创建具体群组/月度 shard 文件；
- `source_chat_id` 如果不在当前 `targets[].target_chat_id` 中，`doctor` 和 GUI 给出 warning；这不是硬错误，因为用户可能在检查历史 tracked DB 或临时归档非 tracked 群，但它通常无法直接解决 tracked 消息上下文缺失；
- Topic 模式下 Topic ID 必须大于 `1`；`1` 是 General，不作为可过滤 Topic；
- 如果 archive root 位于云同步目录，给出 warning；
- 第一阶段不检查 full archive retention；`[full_archive] retention_days` 不受支持，配置解析应直接报错，避免用户误以为归档会自动清理。

### 任务 10：文档

更新：

- `config.example.toml`；
- `docs/configuration.md`；
- GUI config editor must preserve and edit `[full_archive]`，并对不匹配 target 的 `source_chat_id` 显示非阻塞 warning；
- 如果进入用户文档，也同步本地化配置文档；
- 只有当功能进入 release notes 时再更新 README。

如果改 `README.md`，必须遵守 README localization rule。

## 迁移策略

### 现有 tracked DB

除非必要，不迁移现有 tracked DB。

允许：

- 不改 `messages` 和 `media` schema。

首选：

- 只新增 full archive DB 文件。

### 现有 config

如果没有 `[full_archive]`：

- 默认 disabled；
- 不报配置错误；
- 不创建新文件。

### 未来文件夹布局

老用户可能仍然是：

```text
data/tgwatch.sqlite3
data/media/
```

第一阶段不要自动移动这些文件。

新文档可以推荐：

```text
data/tracked/
data/full_archive/
```

但目录迁移应该以后显式做。

## 查询示例

### 查询 tracked message 周围上下文

```sql
ATTACH DATABASE 'data/tgwatch.sqlite3' AS tracked;

SELECT
    a.date,
    a.sender_id,
    COALESCE(t.text, a.text) AS text,
    CASE WHEN t.message_id IS NULL THEN 0 ELSE 1 END AS is_tracked
FROM archive_messages AS a
LEFT JOIN tracked.messages AS t
  ON t.chat_id = a.chat_id
 AND t.message_id = a.message_id
WHERE a.chat_id = :chat_id
  AND a.date BETWEEN :start AND :end
ORDER BY a.date ASC;
```

第一版要把这个查询固化为只读 CLI：

```bash
python -m tgwatch archive-context --config config.toml --chat -1001234567890 --message-id 12345
```

参数：

- `--chat`：目标 tracked message 所在 chat；
- `--message-id`：目标 tracked message ID；
- `--before-minutes`：向前窗口，默认 10，必须大于等于 0；
- `--after-minutes`：向后窗口，默认 5，必须大于等于 0；
- `--topic-id`：可选，只查某个 Topic；传入时必须大于 `1`。

CLI help 也必须明确 `--topic-id` 只接受大于 `1` 的普通 forum Topic ID，并提示 General `1` 需要使用整群查询。不能只写成 positive topic ID，否则真实 QA 时很容易把 General `1` 误填为合法过滤值。

实现边界：

- 只读 tracked DB 和 full archive shards；
- 如果当前 tracked DB 不存在、不可读、缺少 `messages` 表，或 `messages` 表缺少定位目标消息所需的 `chat_id` / `message_id` / `date` 任一列，返回明确 tracked DB 错误；如果 tracked DB 可读但目标 tracked message 不存在，才返回 tracked message not found；
- 如果 full archive manifest 不存在，返回空结果，不创建文件；
- `tracked_ref` 行必须通过 `ATTACH` 取回 tracked text；如果 tracked DB 对应行存在但 `text` 为 `NULL`，这是合法空文本，不能回退显示 archive 侧残留 text；
- 当前 tracked DB 不存在、不可读、缺少 `messages` 表，或 `messages` 表缺少 `chat_id` / `message_id` / `date` / `text` / `replied_text` 任一列时，`archive-context` 仍可返回普通 archive rows，但匹配当前 tracked DB 的 `tracked_ref` 行必须报告明确错误并返回非零；错误不能打印本机路径，查询过程不能创建空 tracked DB；
- 输出 rows 前必须显示实际查询窗口：中心 tracked message 时间点、`since`、`until`、before/after 分钟数，以及当前 Topic filter（未传时显示 whole group）。时间范围和过滤条件分两行输出，避免终端自动换行降低可读性；
- 输出元数据表必须包含 `Target`、`Topic` 和 `Reply` 列：`Target` 用 `*` 标出命令指定的 tracked message；如果目标行不在 archive 结果里，仍可基于 tracked DB 时间点输出上下文，但必须显示 `Target archived row: no`；传入 `--topic-id` 且目标 archived row 实际属于其他 Topic、General 或未知 Topic 时，必须打印 Topic mismatch 诊断并返回非零，提示用户改用正确 Topic 或整群查询；具体 Topic 显示 `topic_id`，`NULL` topic 显示 `-`；`Reply` 列显示 `reply_to_msg_id` / `reply_to_top_id`，没有 reply/thread 线索时显示 `-`，方便整群窗口查询时人工区分多 Topic 和短消息上下文；
- 正文和媒体摘要必须输出在每条消息元数据行后的 `Text:` 缩进行中，并做空白归一化和长度截断，避免真实群聊长文本或长文件名破坏元数据列对齐；`tracked_ref` 行不能显示 archive 侧媒体元数据，即使旧库或损坏库里残留了 `archive_media` 行；
- `tracked_ref` 行如果通过 tracked DB 读到了 `replied_text`，必须在 `Text:` 下方输出独立 `Reply snapshot:` 行，并使用同一套空白归一化和长度截断规则；普通 archive row 第一版不合成 reply snapshot；
- 覆盖窗口内的 shard 如果缺少核心 schema 表，`archive-context` 必须跳过该 shard、报告 error 并返回非零；如果缺少可增量 schema 表（例如 `archive_media`），可以继续返回正文时间线，但必须报告 error 并返回非零，避免用户误以为媒体上下文完整；
- 至少保留一个离线端到端测试：真实创建 tracked DB、manifest、archive shard 和 tracked_ref link，再通过 `archive-context` 读取文件验证输出，避免 CLI monkeypatch 测试与真实 SQLite 行为脱节；
- 不连接 Telegram，不触发 backfill。

### 查询某条 tracked row 附近消息

```sql
SELECT *
FROM archive_messages
WHERE chat_id = :chat_id
  AND topic_id IS :topic_id
  AND date BETWEEN datetime(:tracked_date, '-10 minutes')
               AND datetime(:tracked_date, '+5 minutes')
ORDER BY date ASC;
```

## 发布计划

建议 SemVer 影响：

- Minor release：新增可选全量归档功能，属于向后兼容的增量能力。

建议 changelog：

```text
Add an optional local full-message archive that captures group context in separate SQLite shards and links archived messages back to tracked-user records.
```

## 推出顺序

1. Config parser 和文档。
2. Storage schema、群组月度 shard 选择和单元测试。
3. 默认关闭的 live capture handler。
4. Backfill command。
5. Topic filtering。
6. Topic listing helper。
7. Doctor checks。
8. 真实群组手动验证。

## 已知实现风险

- Forum topic ID 在不同 Telegram message shape 下可能表现不同。
- Topic listing 可能需要权限，或需要 raw MTProto 处理。
- Backfill 限制过宽时可能触发 FloodWait。
- Full archive 写入不能阻塞 tracked push。
- 严格不重复保存 tracked text 会降低 archive shard 单独打开时的可读性。

## 推荐第一版

第一版实现：

- 整群归档；
- 独立 archive DB 文件夹；
- 月度分片；
- 不下载全量媒体；
- 严格不重复保存 tracked text/media；
- 明确的 link table 回连 tracked DB；
- 如果启用 Topic filtering，先支持手动 Topic ID。

这样能最快交付核心价值，同时把 Topic picker 和更复杂的去重体验留到后续。
