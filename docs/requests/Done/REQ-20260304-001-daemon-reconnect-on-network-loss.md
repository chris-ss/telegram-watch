# REQ-20260304-001-daemon-reconnect-on-network-loss

Status: Done
Owner: @yxw
Created: 2026-03-04

## Summary
daemon 模式在网络中断后应自动重连，而非直接崩溃退出。

## Release Impact
- 预计版本号：`1.2.1 -> 1.3.0`
- 预告 changelog 文案：`Add automatic reconnection with exponential backoff when network drops during daemon mode`
> README 中引用的 `pip install ...@vX.Y.Z` 示例只在新版 tag 发布后更新；在 dev 阶段保持指向最新稳定版。

## Motivation
2026-03-04 凌晨 03:13，daemon 进程因网络短暂中断而直接崩溃退出，丢失了后续所有监控。
对于一个需要 7×24 运行的 daemon 来说，瞬时网络故障不应导致进程退出。

## Bug Investigation

### 现场日志（摘要）
```
03:13:54 WARNING Server closed the connection: [Errno 60] Operation timed out
03:13:54 WARNING Attempt 1 at connecting failed: OSError: [Errno 51] Network is unreachable
  ... (6 attempts × 5 rounds, all failed) ...
03:14:29 ERROR   Automatic reconnection failed 5 time(s)
03:14:29 ERROR   ConnectionError: Connection to Telegram failed 5 time(s)
```
进程随后 traceback 退出。

### 根因分析
`runner.py:307-311`，`run_until_disconnected()` 的异常处理：
```python
try:
    await client.run_until_disconnected()
except Exception as exc:
    await _send_error_notification(...)
    raise  # ← 直接 re-raise，进程崩溃
```
Telethon 内部重试耗尽（6 次 × 5 轮 ≈ 36 秒）后抛出 `ConnectionError`，
daemon 捕获后直接 re-raise，进程退出。

### 缺失能力
| 缺失项 | 说明 |
|--------|------|
| 外层重试循环 | 连接断开后不会等待网络恢复再重连 |
| 指数退避 | 不区分瞬时故障（网络断开）和永久故障（认证失败） |
| 客户端重建 | Telethon client 断连后需要重新 `connect()` |
| 相关测试 | 没有 ConnectionError 场景的测试用例 |

## Scope (MVP)
必须做：
- [x] 在 `run_until_disconnected()` 外层加带指数退避的重试循环
- [x] 仅对网络类异常（`ConnectionError`、`OSError`）重试
- [x] 认证类异常（`AuthKeyError` 等）仍然直接退出
- [x] 重连成功后发送通知到 control chat
- [x] 添加 ConnectionError 场景的单元测试

不做（明确排除项）：
- [x] 不改动 Telethon 内部重试逻辑
- [x] 不添加外部进程守护（systemd/launchd）
- [x] 不添加配置项（退避参数暂用合理默认值）

## Functional Requirements
- [x] 网络中断后，daemon 自动等待并重试连接，初始间隔 10s，指数退避至最大 5min
- [x] 每次重试在日志中记录 WARNING，含当前退避时长
- [x] 重连成功后向 control chat 发送恢复通知（含断线时长）
- [x] 遇到非网络类异常（认证失败、被封禁）时仍立即退出并报错

## Non-Functional Requirements
- [x] Mac 本地可运行
- [x] 免费方案
- [x] 隐私/安全：不泄露 session/api_hash
- [x] 退避期间不消耗 CPU（纯 asyncio.sleep）

## Acceptance Criteria (DoD)
- [x] 模拟 `ConnectionError` 后 daemon 不退出，等待并重试
- [x] 重试间隔符合指数退避（10s → 20s → 40s → ... → 300s cap）
- [x] 重连成功后 control chat 收到恢复通知
- [x] `AuthKeyError` 等认证异常仍立即退出
- [x] `pytest tests/` 全部通过，含新增的重连测试
- [ ] `python -m tgwatch doctor --config config.toml` 通过（需 config.toml）

## Implementation Notes (for Codex)
- 主要修改文件：`telegram_watch/runner.py` — `run_daemon()` 函数
- 可能涉及：`telegram_watch/cli.py` — `_run_daemon_command()` 异常处理
- 风险点：重连后 Telethon client 的 event handler 是否仍然有效，需验证
- 需要更新的文档：无（内部行为变更，不影响用户接口）

## What changed

- **`telegram_watch/runner.py`** — 新增 `_run_with_reconnect()` 函数，在 `run_until_disconnected()` 外层实现带指数退避（10s→300s）的重连循环；新增 `_is_auth_error()` 区分网络异常和认证异常；新增 `_send_reconnect_notification()` 在重连成功后通知 control chat。`run_daemon()` 中将直接调用 `run_until_disconnected()` 改为调用 `_run_with_reconnect()`。
- **`tests/test_runner.py`** — 新增 4 个测试：重连重试、认证异常直接退出、退避倍增、`_is_auth_error` 判断逻辑。
