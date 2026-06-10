"""Export all messages from a specific sender in a chat as Markdown.

Designed for "extract one person's full chat history → AI summarization"
(e.g. summarize someone's stated views, surface repeated topics, mine
opinions or share-history). Uses the full-message archive built by
telegram-watch's [full_archive] feature.

Usage:
    python scripts/export_user.py --chat -1001234567890 --sender 12345678
    python scripts/export_user.py --chat -1001234567890 --name "Alice"
    python scripts/export_user.py --chat -1001234567890 --name "Alice" --from 2026-01-01 --to 2026-06-01
    python scripts/export_user.py --chat -1001234567890 --name "Alice" --out reports/alice.md

Requires the venv built by launch_tgwatch.bat. No extra dependencies.

Notes:
    * The chat must be covered by [full_archive] with capture_scope = "whole_group"
      (or a `topics` scope that includes every topic the sender posts in).
    * If the user joined / posted before the archive was enabled, run
      `tgwatch archive-backfill --apply` to fill historical data first.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = ROOT / "data" / "full_archive"
MANIFEST_PATH = ARCHIVE_ROOT / "manifest.sqlite3"

MEDIA_MARKER = " 📎"
TOPIC_LINK_MARKER = " 🔗[topic-link]"


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite file in read-only mode via URI."""
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _list_active_shards(chat_id: int) -> list[tuple[Path, str, str]]:
    """Return [(path, starts_at, ends_at)] for active shards of this chat."""
    if not MANIFEST_PATH.is_file():
        return []
    try:
        with _open_ro(MANIFEST_PATH) as conn:
            rows = conn.execute(
                """
                SELECT path, starts_at, ends_at
                FROM archive_shards
                WHERE chat_id = ? AND status = 'active'
                ORDER BY starts_at ASC
                """,
                (chat_id,),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        print(f"[export_user] manifest read failed: {exc}", file=sys.stderr)
        return []
    result: list[tuple[Path, str, str]] = []
    for row in rows:
        stored = Path(row["path"])
        shard_path = stored if stored.is_absolute() else (ARCHIVE_ROOT / stored)
        if shard_path.is_file():
            result.append((shard_path, row["starts_at"], row["ends_at"]))
    return result


def _resolve_sender(
    sender_id: int | None,
    name: str | None,
    shard_paths: list[Path],
) -> tuple[int, str] | None:
    """Return (sender_id, display_label) or None if not found / ambiguous."""
    if sender_id is not None:
        for shard_path in shard_paths:
            try:
                with _open_ro(shard_path) as conn:
                    row = conn.execute(
                        "SELECT display_name, username FROM archive_senders WHERE sender_id = ?",
                        (sender_id,),
                    ).fetchone()
            except sqlite3.DatabaseError:
                continue
            if row:
                label = row["display_name"] or (
                    f"@{row['username']}" if row["username"] else f"user#{sender_id}"
                )
                return (sender_id, label)
        return (sender_id, f"user#{sender_id}")

    if not name:
        return None
    needle = name.strip().lstrip("@").lower()
    matches: list[tuple[int, str]] = []
    for shard_path in shard_paths:
        try:
            with _open_ro(shard_path) as conn:
                rows = conn.execute(
                    """
                    SELECT sender_id, display_name, username
                    FROM archive_senders
                    WHERE LOWER(COALESCE(display_name, '')) LIKE ?
                       OR LOWER(COALESCE(username, '')) LIKE ?
                    """,
                    (f"%{needle}%", f"%{needle}%"),
                ).fetchall()
        except sqlite3.DatabaseError:
            continue
        for row in rows:
            sid = int(row["sender_id"])
            label = row["display_name"] or (
                f"@{row['username']}" if row["username"] else f"user#{sid}"
            )
            if not any(m[0] == sid for m in matches):
                matches.append((sid, label))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    lines = [f"[export_user] 名字 '{name}' 命中 {len(matches)} 个用户，请用 --sender 明确指定："]
    for sid, label in matches[:20]:
        lines.append(f"  - {label} (sender_id={sid})")
    if len(matches) > 20:
        lines.append(f"  ... 另有 {len(matches) - 20} 个")
    raise SystemExit("\n".join(lines))


def _query_sender(
    shard_paths: list[Path],
    chat_id: int,
    sender_id: int,
    date_from: str | None,
    date_to: str | None,
) -> dict[int | None, list[sqlite3.Row]]:
    """Return the sender's messages grouped by topic_id (None = General)."""
    sql = """
        SELECT
            m.date            AS date,
            m.message_id      AS message_id,
            m.topic_id        AS topic_id,
            m.text            AS text,
            m.has_media       AS has_media,
            m.is_forum_topic_link AS is_topic_link
        FROM archive_messages m
        WHERE m.chat_id = ?
          AND m.sender_id = ?
          AND (? IS NULL OR m.date >= ?)
          AND (? IS NULL OR m.date <  ?)
        ORDER BY m.date ASC, m.message_id ASC
    """
    params: tuple = (chat_id, sender_id, date_from, date_from, date_to, date_to)
    grouped: dict[int | None, list[sqlite3.Row]] = {}
    for shard_path in shard_paths:
        try:
            with _open_ro(shard_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as exc:
            print(
                f"[export_user] shard read failed: {shard_path.name}: {exc}",
                file=sys.stderr,
            )
            continue
        for row in rows:
            grouped.setdefault(row["topic_id"], []).append(row)
    for tid in grouped:
        grouped[tid].sort(key=lambda r: (r["date"], r["message_id"]))
    return grouped


def _topic_label(tid: int | None) -> str:
    return "General" if tid is None else f"Topic {tid}"


def _format_message(row: sqlite3.Row) -> str:
    ts = (row["date"] or "")[:19].replace("T", " ")
    media = MEDIA_MARKER if row["has_media"] else ""
    link_marker = TOPIC_LINK_MARKER if row["is_topic_link"] else ""
    text = (row["text"] or "").strip()
    if not text and not media:
        text = "_(empty / media-only)_"
    return f"- **{ts}**{media}{link_marker}\n  {text}"


def export_markdown(
    chat_id: int,
    sender_id: int,
    sender_label: str,
    grouped: dict[int | None, list[sqlite3.Row]],
    date_from: str | None,
    date_to: str | None,
) -> str:
    total = sum(len(v) for v in grouped.values())
    by_topic_counts = ", ".join(
        f"{_topic_label(t)}={len(v)}" for t, v in sorted(grouped.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))
    )
    lines: list[str] = [
        f"# 群 {chat_id} 中 {sender_label} 的发言导出",
        f"sender_id: {sender_id}",
        f"日期范围: {date_from or '（不限）'} ~ {date_to or '（不限）'} (UTC)",
        f"消息总数: {total}",
        f"按 Topic 分布: {by_topic_counts or '_无_'}",
        "",
    ]
    if total == 0:
        lines.append("_未找到该 sender 在此范围的任何消息。_")
        return "\n".join(lines) + "\n"

    order = sorted(grouped.keys(), key=lambda t: (t is None, t or 0))
    for tid in order:
        rows = grouped[tid]
        lines.append(f"\n## {_topic_label(tid)} ({len(rows)} 条)\n")
        lines.extend(_format_message(r) for r in rows)
    return "\n".join(lines) + "\n"


def _parse_date(raw: str | None, end_of_day: bool = False) -> str | None:
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"日期解析失败（需要 YYYY-MM-DD）: {exc}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt + timedelta(days=1)
    return dt.isoformat()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="导出指定 sender 在某群的全部发言为 Markdown（供 AI 总结 / 观点挖掘）。"
    )
    p.add_argument("--chat", type=int, required=True, help="目标群 chat_id（负数）")
    p.add_argument("--sender", type=int, default=None, help="数字 sender_id")
    p.add_argument("--name", type=str, default=None, help="按 display_name / @username 模糊匹配")
    p.add_argument("--from", dest="date_from", type=str, default=None, help="起始日期 YYYY-MM-DD (UTC, 含)")
    p.add_argument("--to", dest="date_to", type=str, default=None, help="结束日期 YYYY-MM-DD (UTC, 不含次日)")
    p.add_argument("--out", type=Path, default=None, help="输出文件路径，默认 stdout")
    args = p.parse_args(argv)

    if args.sender is None and not args.name:
        p.error("必须提供 --sender 或 --name 其中之一")

    shards = _list_active_shards(args.chat)
    if not shards:
        print(
            f"[export_user] 未找到群 {args.chat} 的归档 shard。"
            f"请确认 [full_archive] enabled=true 且 daemon / archive-backfill 已运行。",
            file=sys.stderr,
        )
        return 2

    resolved = _resolve_sender(args.sender, args.name, [p for p, _, _ in shards])
    if resolved is None:
        print(
            f"[export_user] 在归档中找不到 sender: --sender={args.sender} --name={args.name!r}",
            file=sys.stderr,
        )
        return 3
    sender_id, sender_label = resolved

    date_from = _parse_date(args.date_from, end_of_day=False)
    date_to = _parse_date(args.date_to, end_of_day=True)

    grouped = _query_sender(
        [p for p, _, _ in shards],
        args.chat,
        sender_id,
        date_from,
        date_to,
    )
    md = export_markdown(
        args.chat, sender_id, sender_label, grouped, date_from, date_to
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"[export_user] wrote {args.out} ({len(md)} chars)")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
