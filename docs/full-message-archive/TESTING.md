# 全量消息归档测试文档

## 测试目标

- 证明现有 tracked-user 行为不变。
- 证明 full archive 是 opt-in。
- 证明只有开启后才会创建 archive DB。
- 证明 archive message 能 link 到 tracked message。
- 证明 shard rotation 可用。
- 证明 Topic filter 不会把其他 Topic 的消息漏进来。

## 单元测试

### 配置解析

在 `tests/test_config.py` 增加：

- 缺少 `[full_archive]` 时默认 disabled；
- `enabled` 只接受 TOML boolean，拒绝字符串或数字，避免误启用归档；
- 开启 archive 时必须配置 `source_chat_id`；
- `enabled = true` 且 `capture_scope = "topics"` 时 `topic_ids` 必须非空；`enabled = false` 时允许保存未填 Topic 的草稿配置；
- 非法 Topic ID 报错；`topic_ids` 必须大于 `1`，`1` 代表 General，只能通过整群归档覆盖；
- 非法 shard threshold 报错；
- 合法 whole-group 配置可解析；
- 合法 topic 配置可解析。

### GUI 配置读写

在 `tests/test_gui_runner.py` 覆盖：

- GUI normalize 包含 full archive 默认值；
- GUI 保存后 TOML 保留 `[full_archive]`；
- `topic_ids` 在 GUI 字符串和 TOML list 之间正确转换；
- GUI 拒绝 `topic_ids` 中的 `0`、`1` 或负数，避免保存后生成 config parser 不接受的配置；
- 开启 full archive 时 `source_chat_id = 0` 报错；
- GUI 必须保留 `backfill_limit_messages = 0`，不能把安全 no-op 值改回默认 `10000`；
- i18n 文案包含 full archive 字段。

### 存储 schema

新增 `tests/test_full_archive_storage.py`：

- manifest schema 创建可重复执行；
- shard schema 创建可重复执行；
- archive 写连接启用 WAL 和 `busy_timeout = 5000`，只读连接至少保留 `busy_timeout = 5000`，为 live capture、relink 和 backfill 的并发 SQLite 写入提供基础锁等待能力；
- 新建 shard 在 manifest 中保存 root-relative path，且移动整个 `full_archive/` 文件夹后 status/context/repair 仍能解析 shard；
- 旧版 manifest 如果已经保存 absolute shard path，读取层仍要兼容，不能把现有归档误判为缺失；
- archive message upsert 可用；
- 重复 `(chat_id, message_id)` 会更新现有行；
- 普通 archive row 会保存轻量媒体元数据，但不下载媒体文件；
- relink 成 `tracked_ref` 后删除 archive 侧媒体元数据，避免和 tracked DB 媒体重复；
- 已经是 `tracked_ref` 的 row 后续遇到 archive-only 观测时不能降级为 `archive`，即使当前 tracked DB 暂时不可解析，也不能重新保存正文或 archive 侧媒体元数据；
- tracked link insert 可用；
- context window query 按 date 排序；
- context window query 不传 `topic_id` 时返回整群时间线，传入 `topic_id` 时只返回该 Topic；
- 删除整个 `full_archive.root_dir` 后，现有 tracked DB 必须仍能重新打开并读取原 tracked message；测试要先创建 archive/tracked link，再删除 archive root，证明删除归档不会破坏 tracked 存储。

### Shard rotation

测试：

- 第一条消息创建月度 shard；
- 同月消息复用 shard；
- 下月消息创建新 shard；
- 达到条数阈值后创建序号 shard；
- 通过 mock file size 测试达到大小阈值后创建序号 shard。
- 通过 SQLite WAL sidecar 文件测试达到大小阈值后创建序号 shard，避免只看主 `.sqlite3` 文件；
- 同一群组、同一月份、不同 Topic ID 的消息复用同一个群组月度 shard。

### Topic 归一化

使用 fake message objects：

- 非 forum 消息 `topic_id = None`；
- forum topic 消息带 `reply_to_top_id > 1` 时映射到对应 topic；
- forum topic linkage 没有 `reply_to_top_id` 但带 `reply_to_msg_id > 1` 时，第一阶段 best-effort 映射到 `reply_to_msg_id`，同时保留原始 reply 字段；
- General topic 或未知 topic 存 `NULL`；
- 原始 reply 字段被保留。

### Link 行为

使用临时 tracked DB 和 archive DB：

- tracked DB 中存在 `(chat_id, message_id)`；
- archive persistence 会创建 link row；
- archive query 通过 `ATTACH DATABASE` 能读到 tracked text；
- 新写入的 tracked DB link 使用 root-relative path，移动同一个项目或 `data/` 目录后仍能解析 `tracked_ref`；
- tracked DB 路径丢失时降级处理，不崩溃。
- `archive-context` 能围绕 tracked message 时间点返回前后窗口；
- `archive-context` 对 `tracked_ref` 行显示 tracked DB 正文，而不是重复 archive text。

## 集成测试

### 现有行为回归

运行现有测试：

```bash
pytest tests/
```

预期：

- 除非 config dataclass 构造参数变化，否则现有 tracked-user 测试不应该需要行为更新。

### Live capture handler

新增 runner 级单元测试：

- full archive disabled 时不注册、不写 archive；
- enabled whole-group 时，非 tracked 用户消息也会写入 archive；
- Topic 模式下，只写入配置的 Topic；
- Topic 模式下，同一个 `_FullArchiveHandler.handle()` 入口必须同时证明：配置 Topic 会写入，其他 Topic 和 General/未知 Topic 不会混入该 Topic 归档；
- full archive enabled 时同时注册 NewMessage 和 MessageEdited handler；
- daemon startup preflight 发现 archive degraded 时，不注册 full archive NewMessage / MessageEdited handler，并且 tracked handler 的 post-persist relink 被禁用；现有 tracked-user handler 仍注册、daemon 仍可启动；
- edited-message 会更新已有 archive-only row，且不增加 manifest message count；
- edited-message 到达已有 `tracked_ref` row 时保持 `tracked_ref`，不写回 archive 正文或 archive 侧媒体元数据，且不重复增加 manifest message count；
- archive 写入异常只记录日志，不向外抛出；
- tracked handler 写入 tracked DB 后，即使 archive relink 失败，也必须继续把消息送入 realtime queue，不能让 full archive 失败拖垮 tracked-user 推送链路；
- tracked handler 不能等待 archive relink 完成才把消息送入 realtime queue；测试应覆盖 relink 挂起时 realtime queue 仍立即收到 tracked 消息；
- tracked handler 的后台 relink task 必须被 pending task set 持有，完成后自动移除，避免 daemon 长时间运行时 task 生命周期不明确；
- daemon shutdown drain 必须短时间等待 pending relink 完成；完成时 task set 清空，超时或 task 已取消时记录 warning 且不无限阻塞退出；
- 删除整个 `full_archive.root_dir` 后，下一次 live/backfill 持久化必须能重新创建 manifest 和 shard，并从新的空归档状态开始计数；
- 删除整个 `full_archive.root_dir` 后，通过真实 `_FullArchiveHandler.handle()` 入口再次写入，必须重新创建 manifest 和 shard；
- 删除整个 `full_archive.root_dir` 后，通过真实 `run_archive_backfill(..., apply=True)` 入口再次写入，必须重新创建 manifest 和 shard，并且旧归档行不应出现在新 shard 中；
- 删除整个 `full_archive.root_dir` 后，如果 live capture 或 backfill 重新遇到 tracked DB 已有的消息，仍必须写成 `tracked_ref`，不重复保存 tracked 正文或 archive 侧媒体元数据；
- tracked handler 写入 tracked DB 后，会把同一 archive row relink 为 `tracked_ref`。
- runner 级离线测试覆盖真实 handler 顺序：full archive handler 先写 archive-only row，tracked handler 随后写 tracked DB 并 relink；最终 archive row 不保留 tracked 正文，也不保留 archive 侧媒体元数据。
- runner 级离线测试覆盖反向顺序：tracked handler 先写 tracked DB 并创建 `tracked_ref`，full archive handler 后到达同一消息时保持幂等；最终 archive row 仍不保留 tracked 正文、archive 侧媒体元数据，且 manifest message count 不重复增加。
- Topic 模式下，tracked handler 的 post-persist relink 必须对未配置 Topic 和 General/未知 Topic no-op：tracked DB 可保存这些 tracked 消息，但 full archive 不能因此创建 `tracked_ref` 或 archive root。

### Doctor

增加 doctor 输出测试：

- archive disabled 时不要求 archive folder 存在；
- archive enabled 时创建/检查 root dir，并用本地探针验证可写；
- archive enabled 时创建/检查 `root_dir/shards/` 并用本地探针验证可写，但不创建具体 group/month shard 文件；
- 只读或写入探针失败的 archive dir 报 failure；
- 云同步目录 warning 仍按现有规则出现，并覆盖 macOS 常见的 `~/Library/CloudStorage/iCloud Drive/`、OneDrive 和 Google Drive 路径。

### Backfill dry run

Backfill 应支持 dry-run：

```bash
python -m tgwatch archive-backfill --config config.toml --limit 100 --dry-run
```

测试：

- dry run 不写 DB；
- `--dry-run` 是受支持的显式模式，行为等同于省略模式参数；
- `--dry-run` 和 `--apply` 互斥，不能让冲突模式进入真正的 Telegram backfill；
- `archive-backfill --help` 必须说明 `--limit` 是非负整数，并且 `0` 是不连接 Telegram、不创建 archive root 的 no-op；
- `archive-backfill --limit <0>` 必须在 CLI 层返回可读错误，不连接 Telegram、不创建 archive root，也不打印 traceback；
- 有效 limit 为 `0` 时成功 no-op，不连接 Telegram、不创建 archive root；
- stats 输出 scanned/matched/skipped 数量。
- apply 会写入 archive shard；
- 同一批消息重复 apply 不增加 archive row 数、manifest message count 或 link row 数；
- 同一批消息重复 apply 时，CLI/backfill stats 不把已存在 row 再计入 archived rows 或 tracked links，而是计入 updated rows；
- backfill 首次遇到 tracked DB 已存在的同一 `(chat_id, message_id)` 时，直接写入 `tracked_ref`，stats 计入 tracked links，不计入 archived rows；
- backfill 把已有 archive-only row 升级为 `tracked_ref` 时，stats 计入 tracked links，不增加 archive row 数或 manifest message count；
- Topic scope 在 backfill 中和 live capture 一致：配置 Topic 写入，其他 Topic、General 或未知 Topic 计入 `skipped_scope` 且不写入 archive shard；
- full archive 未开启时，CLI 级 `archive-backfill` 返回可读错误，不连接 Telegram、不创建 archive root，也不打印 traceback。
- 历史消息缺少稳定 `(chat_id, message_id)` 身份时计入 `skipped_invalid` 并继续处理后续消息，不中断整段 backfill；
- `run_archive_backfill(..., apply=True)` 入口层必须证明 Telegram service messages 会归一化为 `message_kind = "service"` 并进入时间线；如果没有正文，`text` / `raw_text` 可以为 `NULL`，不能因为缺少正文跳过稳定身份的 service message；
- 超过 1,000 条的 backfill 会给 Telethon `iter_messages` 传 `wait_time=1.0`。
- backfill 迭代中遇到 `FloodWaitError` 时会 sleep 后用最后已处理的 `message_id` 继续，不重复处理已扫描消息；首次 FloodWait 后，后续请求会切换到 `wait_time=1.0`。
- apply 模式下 FloodWait 恢复不能重复写入已持久化消息，manifest message count 和 archive rows 必须保持准确。
- `archive-backfill --apply` 在连接 Telegram 前执行只读 health preflight：现有归档为 degraded 时返回可读错误，不调用 backfill、不写入 archive；
- manifest 不存在或整删归档后的 empty 状态不阻止 `archive-backfill --apply`，且 preflight 本身不能创建 archive root。
- 有效 limit 为 `0` 时即使旧归档是 degraded，也保持成功 no-op，不执行 health preflight、不连接 Telegram、不写入 archive；
- `archive-backfill --limit <0>` 的无效参数错误优先于 archive health preflight，避免 degraded 旧归档掩盖 CLI 参数错误。

### Archive status

`archive-status` 是只读诊断命令。测试应覆盖：

- full archive disabled 时输出 disabled，且不创建 archive root；
- manifest 不存在且没有 shard 文件时输出 empty 状态、命令成功，且不创建 archive root；这不是 degraded；
- manifest 不存在但 `root_dir/shards/` 下仍有 shard DB 时输出 degraded、返回非零，不能把孤儿归档数据当成 empty；
- manifest 存在但 `root_dir/shards/` 下有未登记 shard DB 时输出 degraded、返回非零，不能静默忽略隐藏归档数据；
- manifest 中登记的 shard 缺失时，missing shard 计数增加；
- `doctor` 和 GUI 在 full archive 开启但 `source_chat_id` 不属于当前 configured targets 时给出 warning，不阻止 watcher 启动或保存配置；
- manifest 中登记的 tracked DB link 数量会被统计；
- manifest 缺少可增量 schema 表（例如 `tracked_db_links`）时，status 标记 degraded，不能把缺失表静默显示为 0 个 link；
- 当前配置的 tracked DB 已登记且包含 `archive-context` 需要的 tracked `messages` schema 时，status 显示 linked/readable；
- manifest/shard 已有归档消息但没有任何 active tracked DB link 时，status 标记 degraded，且不打印路径；这个判断要同时覆盖传入当前 tracked DB 路径和不传 tracked DB 路径的 storage 层调用；
- manifest 有历史 tracked DB link 但当前配置 DB 未登记时，status 标记 degraded 且不打印路径；
- 当前配置 tracked DB 已登记但文件不可读或不存在时，status 标记 degraded，且不创建空 tracked DB；
- 当前配置 tracked DB 已登记但只是空 SQLite 文件、缺少 tracked `messages` 表，或 `messages` 表缺少 `chat_id`、`message_id`、`date`、`text`、`replied_text` 任一列时，status 标记 degraded；
- shard 写入失败时不能创建 tracked DB link，避免 manifest link 统计污染；
- 可读 shard 中统计 archive media metadata 行数；
- 可读 shard 中统计实际消息数、`archive` 行、`tracked_ref` 行和 link 行；
- 可读 shard 中 `tracked_ref` 行缺少 `tracked_db_path`、`tracked_message_chat_id`、`tracked_message_id` 任一字段时，status 显式标记 incomplete tracked_ref metadata；CLI 端到端必须返回非零并输出可读原因；
- 可读 shard 中 `tracked_ref` 行数和 `archive_tracked_links` 行数不一致时，status 标记 degraded；
- 可读 shard 中 `tracked_ref` 行数和 link 行数相同、但 link 内容指向 stale/wrong tracked DB 或 message 时，status 也标记 degraded；
- 可读 shard 中 `tracked_ref` 行残留 archive 侧 `text` / `raw_text` 正文时，status 标记 degraded；
- 可读 shard 中 `tracked_ref` 行残留 archive 侧 `archive_media` 元数据时，status 标记 degraded；
- manifest message count 与 shard 实际消息数不一致时标记 degraded；
- manifest file size 与 shard 实际文件大小不一致时不单独标记 degraded，因为 WAL/checkpoint 会让文件大小自然变化；这里的实际文件大小包含 `.sqlite3`、`-wal`、`-shm`；`archive-repair --apply` 仍可同步该 metadata；
- 可读 shard 缺少必需索引时，missing index 计数增加并标记 degraded；
- 可读 shard 缺少可增量 schema 表时，missing schema table 计数增加并标记 degraded；
- parser 支持 `archive-status --config config.toml`。

### Archive repair

`archive-repair` 是显式修复命令。测试应覆盖：

- 默认 dry-run，不修改 shard；
- `--apply` 只创建缺失的必需索引；
- `--apply` 会补齐可增量创建的 manifest schema 表，例如 `tracked_db_links`；
- `--apply` 会补齐可增量创建的 shard schema 表，例如 `archive_media`；
- 核心表缺失时不会凭空创建空表伪装修复，而是报告 skipped；
- `--apply` 会把 manifest 的 message count/file size 同步为 shard 实际值；
- dry-run 会报告可由完整 `tracked_ref` 元数据重建的 missing/stale tracked link 行，且不修改 shard；
- `--apply` 会重建 `archive_tracked_links`，修复后 status 不再因为 tracked_ref/link count 或 content mismatch degraded；
- `--apply` 会清空 `tracked_ref` 行下残留的 archive 侧 `text` / `raw_text` 派生正文，修复后 status 不再因此 degraded；
- `--apply` 会删除 `tracked_ref` 行下残留的 archive 侧 `archive_media` 派生元数据，修复后 status 不再因此 degraded；
- `--apply` 不能猜测修复 incomplete tracked_ref metadata；测试应证明这类损坏仍由 status 明确报告，repair 本身返回 errors/skipped，并且 CLI 端到端返回非零、输出可读原因；
- full archive disabled 时返回可读错误；
- manifest 不存在且没有孤儿 shard 时成功返回 no-op，且 CLI 不创建 archive root；
- manifest 不存在但存在孤儿 shard 时返回非零、输出不可自动修复原因，且不创建或删除文件；
- manifest 存在但有未登记 shard DB 时返回非零、输出不可自动修复原因，且不创建、登记或删除文件；
- 缺失 shard 不会被创建，会报告 skipped reason，CLI 返回非零。
- parser 支持 `archive-repair --prune-missing-shards`；
- `archive-repair --help` 必须说明 `--prune-missing-shards` 默认只做 dry-run，只有和 `--apply` 一起使用才会写入 manifest；
- `--prune-missing-shards` dry-run 只报告可清理的 stale manifest 行，不修改 manifest；
- `--prune-missing-shards --apply` 只删除已经缺失文件对应的 manifest 行，不删除任何 shard 文件、tracked DB 或媒体文件；
- prune 后缺失 shard 不再计入 skipped，CLI 返回成功。

### Archive context

`archive-context` 是只读上下文查询命令。测试应覆盖：

- parser 支持 `archive-context --config config.toml --chat ... --message-id ...`；
- `archive-context --help` 必须说明 `--topic-id` 只接受大于 `1` 的普通 forum Topic ID，并提示 General `1` 使用整群查询；
- full archive disabled 时返回可读错误；
- `--before-minutes` 和 `--after-minutes` 为负数时返回可读错误且不创建 archive root；
- `--topic-id` 传入 `0`、`1` 或负数时返回可读错误且不创建 archive root；
- 当前 tracked DB 不存在、不可读、缺少 `messages` 表，或缺少定位目标消息所需的 `chat_id` / `message_id` / `date` 任一列时，返回明确 tracked DB 错误，不创建空 tracked DB，也不打印本机路径；
- tracked DB 可读但目标 tracked message 不存在时，才返回 tracked message not found；
- manifest 不存在时返回空结果且不创建 archive root；这也是删除整个 `full_archive.root_dir` 后的预期 `archive-context` 行为：tracked DB 可读时仍能定位目标消息时间，但只显示没有归档上下文和 `Target archived row: no`；
- CLI 级真实 SQLite 测试必须覆盖 `archive-context --topic-id <topic_id>` 只输出该 Topic 的 archive/tracked_ref 行，不显示其他 Topic 或 General/未知 Topic 行；
- `archive-context --topic-id <topic_id>` 如果目标 archived row 存在但属于其他 Topic、General 或未知 Topic，必须打印 Topic mismatch 诊断并返回非零，避免用户误把错误 Topic 的邻近消息当作目标上下文；
- 查询结果按时间排序；
- 查询窗口跨多个 shard 时合并结果并按时间排序；
- 大窗口包含大量带媒体元数据的消息时，`archive-context` 不触发 SQLite expression depth/variable limit；
- 输出中包含实际查询窗口、中心 tracked message 时间点和 Topic filter，且时间范围与过滤条件分行显示；
- 输出元数据表包含 Target、Topic 和 Reply 列，目标 tracked message 出现在 archive 结果中时用 `*` 标记；目标行不在 archive 结果中时显示 `Target archived row: no`；具体 Topic 显示 ID，`NULL` Topic 显示 `-`，reply/thread 字段显示 `reply_to_msg_id` / `reply_to_top_id`，缺失时显示 `-`；
- 长正文和长媒体摘要输出在独立 `Text:` 行，并被归一化、截断，不能破坏元数据列对齐；
- 覆盖窗口的 shard 缺失或不可读时报告 skipped shard，CLI 返回非零；
- manifest/schema 读取失败时输出错误原因，CLI 返回非零；
- 覆盖窗口的 shard 缺少核心 schema 表时报告 skipped/error，CLI 返回非零；
- 覆盖窗口的 shard 缺少可增量 schema 表（例如 `archive_media`）时仍可返回正文行，但必须报告 error，CLI 返回非零，避免静默丢失媒体上下文；
- 普通 archive row 的媒体元数据会出现在 `archive-context` 输出中；
- `tracked_ref` 行通过 attached tracked DB 补回正文。
- `tracked_ref` 行如果 attached tracked DB 中有 `replied_text`，输出中必须包含独立 `Reply snapshot:` 行，且该行同样被归一化、截断；
- `tracked_ref` 行即使残留 archive 侧 `archive_media` 行，也不能在 `archive-context` 中显示这些媒体元数据；
- `tracked_ref` 行记录的 `tracked_db_path` 与当前 config tracked DB 不一致时，不允许用当前 DB 解析，并返回非零；
- `tracked_ref` 行对应 tracked DB 行存在但 `text` 为 `NULL` 时，不误报为 unresolved，也不能回退显示 archive 侧残留 text。
- `tracked_ref` 行无法解析到 tracked DB 对应行时输出错误原因，CLI 返回非零。
- 当前 tracked DB 不存在、不可读、缺少 `messages` 表或缺少 `chat_id` / `message_id` / `date` / `text` / `replied_text` 任一列时，`archive-context` 对匹配当前 DB 的 `tracked_ref` 行输出明确 tracked DB 错误，CLI 返回非零；同时不创建空 tracked DB、不打印本机路径，并继续返回可读的普通 archive rows；
- 至少一个离线端到端 CLI 测试必须使用真实 SQLite 文件：创建 tracked DB、archive manifest、archive shard、普通 archive row、tracked_ref row 和媒体元数据，再调用 `_run_archive_context_command` 验证 link、Target、Topic、Reply、Text 和媒体摘要一起工作。

## 真实 Telegram 验证

这些检查需要真实 Telegram user account，不应在 CI 中运行。

### 验证前置条件

准备一个小型测试群，最好是 forum-enabled group：

- `config.toml` 已能通过 `python -m tgwatch doctor --config config.toml`；
- 测试群的 `target_chat_id` 已配置在 `targets` 中；
- 至少一个账号是 tracked user，另一个账号或群成员不是 tracked user；
- `[full_archive] enabled = true`，`source_chat_id` 指向同一个测试群；
- 如果测试 Topic，先用 `list-topics` 找到 Topic ID，或手动确认 Topic root ID；
- 验证前备份或删除旧的测试 `full_archive.root_dir`，避免旧数据影响判断。

每轮真实验证都先跑：

```bash
python -m tgwatch doctor --config config.toml
python -m tgwatch archive-status --config config.toml
```

`archive-status` 如果是 `empty` 或 `ok` 可以继续；如果是 `degraded`，必须先解释原因，必要时运行 `archive-repair --dry-run`，不能带着未知损坏继续做通过结论。

`config.example.toml` 必须保持 full archive disabled，且 `source_chat_id` 只能作为注释示例出现；示例配置解析后 `full_archive.source_chat_id` 应为 `None`，避免用户只把 `enabled` 改成 `true` 就拿占位群号开始真实 QA。

### 整群 live capture

1. 配置：
   - `capture_scope = "whole_group"`；
   - `topic_ids = []`；
   - `backfill_limit_messages = 0`，避免 daemon 验证前误做历史回填。
2. 启动 daemon：

   ```bash
   python -m tgwatch run --config config.toml
   ```

3. 在测试群发送三条消息：
   - 非 tracked 用户发送一条普通文本，例如 `archive live non-tracked <timestamp>`；
   - tracked 用户发送一条普通文本，例如 `archive live tracked <timestamp>`；
   - tracked 用户发送一条带媒体或 reply 的消息，如果测试条件允许。
4. 停止 daemon 后运行：

   ```bash
   python -m tgwatch archive-status --config config.toml
   python -m tgwatch archive-context --config config.toml --chat <target_chat_id> --message-id <tracked_message_id> --before-minutes 10 --after-minutes 5
   ```

5. 通过条件：
   - 非 tracked 消息出现在 full archive shard，不出现在 tracked DB；
   - tracked 消息出现在 tracked DB；
   - full archive 中同一 tracked 消息是 `tracked_ref`，没有重复保存 tracked 正文或 archive 侧媒体元数据；
   - `archive-status` 显示 current tracked DB linked/readable；
   - `archive-context` 能看到 tracked 消息前后的非 tracked 上下文；
   - 控制群推送、报告和 realtime 行为与开启 full archive 前一致。

### Topic capture

1. 使用 forum-enabled group。
2. 运行：

   ```bash
   python -m tgwatch list-topics --config config.toml --chat <target_chat_id>
   ```

3. 配置：
   - `capture_scope = "topics"`；
   - `topic_ids = [<topic_id>]`；
   - `source_chat_id` 仍指向同一个测试群。
4. 启动 daemon。
5. 在配置 Topic 里发送一条非 tracked 消息和一条 tracked 消息。
6. 在另一个 Topic 或 General 里发送一条明显不同的消息。
7. 停止 daemon 后运行 `archive-status` 和 `archive-context --topic-id <topic_id>`。
8. 通过条件：
   - 配置 Topic 内的消息被归档；
   - 其他 Topic / General 的测试消息没有进入该 Topic 查询结果；
   - `archive-context` 输出的 `Topic` 和 `Reply` 列能帮助人工确认 Topic/thread 归类；
   - 如果 Topic ID 归类和 Telegram UI 不一致，记录为真实 Telegram QA 风险，不能宣称 Topic capture 完成。

### Topic listing

如果实现了：

```bash
python -m tgwatch list-topics --config config.toml --chat -100...
```

确认：

- 输出包含 Topic ID 和标题；
- 输出包含归档配置建议：普通 Topic 标记可用于 `topic_ids`，General `1` 标记为 `whole_group`，并提示不要把 `1` 写入 `full_archive.topic_ids`；
- 权限、entity 解析失败、API 缺失或签名差异可读且非致命，CLI 输出 fallback 说明并非零退出；
- 不打印 secrets。

### Backfill

1. 先 dry-run：

   ```bash
   python -m tgwatch archive-backfill --config config.toml --limit 20 --dry-run
   ```

2. 确认输出只显示统计，不创建新 archive rows。
3. apply 小批量：

   ```bash
   python -m tgwatch archive-backfill --config config.toml --limit 20 --apply
   python -m tgwatch archive-status --config config.toml
   ```

4. 再跑一次同样命令：

   ```bash
   python -m tgwatch archive-backfill --config config.toml --limit 20 --apply
   python -m tgwatch archive-status --config config.toml
   ```

5. 通过条件：
   - 第一次 apply 后 archive rows / tracked links 增长符合扫描结果；
   - 第二次 apply 不重复增加 `archive_messages` 行、manifest message count 或 tracked link 行；
   - 第二次统计中重复消息只计入 updated rows，不计入新的 archived rows 或 tracked links；
   - 如果触发 FloodWait，命令能 sleep 后从上次 offset 继续，并在同一次 backfill 后续请求降速；
   - `archive-context` 能围绕一个历史 tracked message 读出 backfill 得到的上下文。

### 真实验证结论记录

真实 Telegram QA 完成后必须记录：

- 测试日期和 tgwatch commit；
- Telegram/Telethon 版本；
- 测试群是否 forum-enabled；
- 是否验证 whole-group live capture；
- 是否验证 Topic live capture；
- 是否验证至少一类 Telegram service message 进入时间线，或明确记录未验证；
- 是否验证 backfill 幂等；
- `doctor`、`archive-status`、`archive-context` 的结论；
- `archive-context` 遇到 tracked DB 不可读、路径失效或旧 schema 时的诊断是否清楚，不能把读取失败误报为 tracked message 不存在；
- 任何 FloodWait、Topic 归类偏差、权限问题或未验证项。

记录时先生成草稿：

```bash
python -m tgwatch archive-qa-init --config config.toml
```

该命令会把 [真实 Telegram QA 记录模板](REAL_TELEGRAM_QA_TEMPLATE.md) 复制到 `reports/full_archive_qa/` 下；`reports/` 已被 `.gitignore` 排除，适合保存脱敏后的本地验证记录。记录文件必须脱敏，不得写入手机号、`api_hash`、session 路径、私密群名、真实用户昵称或未脱敏消息正文。如果不用命令，也可以手动复制模板，但不要把填好的真实 QA 记录放回 `docs/` 目录。

测试必须同时证明：

- `archive-qa-init` 能从 package data 读取模板；
- 测试直接检查 `telegram_watch/templates/REAL_TELEGRAM_QA_TEMPLATE.md` 作为 package data 文件存在，不能只依赖 `_archive_qa_template_text()` 的 docs fallback；
- `pyproject.toml` 必须声明 `telegram_watch = ["templates/*.md"]` package data，避免源码树测试通过但安装包漏带真实 QA 模板；
- `docs/full-message-archive/REAL_TELEGRAM_QA_TEMPLATE.md` 与 `telegram_watch/templates/REAL_TELEGRAM_QA_TEMPLATE.md` 内容逐字一致，避免维护者只改文档副本或只改发布副本；
- 模板必须包含 Context / tracked DB 诊断章节，覆盖目标查询失败和 `tracked_ref` 解析失败两个层级；
- 模板必须在数据库可管理性章节要求记录整删 root 后重建时 `tracked_ref` 去重仍然成立，不能只记录“文件能重建”；
- `_archive_qa_template_text()` 在 package data 缺失时必须抛出错误，并且 `archive-qa-init` 命令层必须返回可读失败、退出码为 `2`、不创建草稿文件，不能回退到源码树 `docs/` 副本；
- `archive-qa-init --help` 必须提示自定义输出路径应放在 `reports/` 或其他 gitignored 位置，不能放入 `docs/`；
- `archive-qa-init --output <repo>/docs/...` 必须返回可读错误，且不能创建文件；
- 当前 config 的 full archive 仍是 disabled 时，`archive-qa-init` 可以生成草稿，但必须在终端输出 warning，避免把 disabled 状态误当成真实 QA 起点；
- 生成草稿本身必须记录 `archive-qa-init` 执行时 `full_archive.enabled` 是 `true` 还是 `false`，避免后续只看文件时丢失初始配置状态；
- 生成草稿必须自动填写 `tgwatch commit`：git checkout 中写短 SHA，dirty 工作区只写 `dirty` 标记，不展开文件列表；非 git 环境写 `unknown`；
- 生成草稿必须自动填写 Python 版本和 Telethon 版本；Telethon 无法读取版本时写 `unknown`；
- 生成草稿必须自动填写不泄露具体群号的配置摘要：`capture_scope`、`topic_ids` 数量、`backfill_limit_messages`、`source_chat_id` 是否已配置以及是否匹配当前 targets；
- 生成草稿不会创建 `data/full_archive`，也不会把当前配置里的 `api_hash`、session 路径或其他 Telegram secret 写入草稿/打印到终端；测试应使用唯一 sentinel secret 验证没有泄漏真实配置值。

没有这份记录时，只能说“离线测试通过”，不能说 full archive 已经真实端到端可交付。

### CR 结论规则

每轮实现后的 CR 需要独立给出结论，不能只引用测试是否通过。CR 结论只能使用已经被证据证明的最高层级：

- `离线通过，待真实 QA`：离线测试、默认关闭、doctor/status 等检查通过，但没有真实 Telegram QA 记录；
- `本机可验收，待真实 QA`：真实 `config.toml` 上的本机启停、整删归档、status/context/repair 检查通过，但没有 live Telegram QA 记录；
- `真实端到端可交付`：已有脱敏 QA 记录证明 whole-group live capture、必要 Topic capture、backfill 幂等和 context 查询通过。

如果 CR 发现文档、实现、测试或验证证据不一致，必须先修复；暂时不能修复的问题要进入“最终风险记录”或 Todoist 后续任务，不能被一句“测试通过”覆盖。

## 性能测试

使用合成数据，不用 Telegram：

- 插入 10,000 条消息；
- 插入 100,000 条消息；
- 本地时间允许时，可选插入 500,000 条消息。

衡量：

- insert throughput；
- 正负 10 分钟 context query；
- shard open/close overhead；
- manifest update cost。

验收目标：

- 对 indexed date range 的常规窗口查询，本地磁盘上应低于 200 ms；
- 单条 archive message 插入不应阻塞 tracked push path。

自动化测试第一版覆盖：

- 10,000 条合成 archive rows 的窗口查询；
- 整群窗口查询的 `EXPLAIN QUERY PLAN` 使用 `idx_archive_messages_chat_date`；
- Topic 窗口查询继续依赖 `idx_archive_messages_scope_date`；
- 正负 10 分钟窗口查询低于 200 ms；
- live archive capture/relink 通过线程入口执行 SQLite 写入，避免直接占用 event loop。

100,000 / 500,000 条属于本地手动 benchmark，release 前按时间和磁盘条件执行。

## 迁移测试

### 旧 config

使用没有 `[full_archive]` 的 config。

预期：

- 成功解析；
- 不创建 archive 文件；
- 现有命令继续运行。

### 旧 tracked DB

使用只包含当前 `messages` 和 `media` 表的 DB。

预期：

- tracked DB 仍可读；
- archive link 检查可用；
- 不执行破坏性迁移。

## 失败测试

- archive root 父目录无权限；
- shard 文件被锁；
- manifest 损坏或不可访问；
- link query 时 tracked DB 丢失；
- topic listing RPC 失败；
- backfill 触发 FloodWait。

预期行为：

- full archive 报 degraded 或 failed 状态；
- tracked-user capture 和 push 尽可能继续运行；
- 不记录 secrets。

## 验收清单

- [ ] 现有测试通过。
- [ ] Config tests 覆盖 disabled/default 行为。
- [ ] Archive storage tests 通过。
- [ ] Shard rotation tests 通过。
- [ ] Link query test 通过 `ATTACH DATABASE`。
- [ ] Archive context 能围绕 tracked message 输出上下文窗口。
- [ ] Doctor 能报告 archive storage readiness。
- [ ] Archive status 能只读报告 manifest/shard/link 健康状态。
- [ ] Live whole-group capture 已验证。
- [ ] Live topic capture 已验证，或明确延期。
- [ ] Backfill idempotency 已验证。
- [ ] 未加入 AI dependency。
- [ ] 未加入全量媒体下载。
- [ ] 现有 tracked DB migration 保持向后兼容。
- [ ] `archive-qa-init` 已生成 gitignored 的真实 Telegram QA 记录草稿。
- [ ] 没有真实 Telegram QA 记录时，交付结论只写“离线测试通过”，不能写“端到端可交付”。
- [ ] CR 结论按证据层级书写；没有真实 Telegram QA 记录时只能写“离线通过，待真实 QA”或“本机可验收，待真实 QA”。
