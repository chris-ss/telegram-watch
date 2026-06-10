"""Export one local calendar day's full-archive messages as Markdown.

Designed for the daily digest -> AI analysis workflow. By default, ``--day``
means an Asia/Shanghai calendar date, while archive timestamps remain stored
and queried as UTC.

Usage:
    python scripts/export_day.py --chat -1001234567890
    python scripts/export_day.py --chat -1001234567890 --day 2026-06-06
    python scripts/export_day.py --chat -1001234567890 --day 2026-06-06 --timezone Asia/Shanghai
    python scripts/export_day.py --chat -1001234567890 --day 2026-06-06 --out reports/day-2026-06-06.md
    python scripts/export_day.py --chat -1001234567890 --day 2026-06-06 --topics 9001 9002

Requires the venv built by launch_tgwatch.bat. No extra dependencies.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_watch.config import Config, TargetGroupConfig, load_config

ARCHIVE_ROOT = ROOT / "data" / "full_archive"
MANIFEST_PATH = ARCHIVE_ROOT / "manifest.sqlite3"
DEFAULT_TIMEZONE = "Asia/Shanghai"

MEDIA_MARKER = " [media]"
TOPIC_LINK_MARKER = " [topic-link]"


@dataclass(frozen=True)
class ExportContext:
    config: Config | None
    target: TargetGroupConfig | None
    tracked_user_ids: frozenset[int]
    tracked_aliases: dict[int, str]
    media_by_message: dict[tuple[int, int], list[sqlite3.Row]]


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite file in read-only mode via URI."""
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _utc_day_window(local_day: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    local_start = local_day.astimezone(tz).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _find_shards(chat_id: int, starts_at: datetime, ends_at: datetime) -> list[Path]:
    """Return shard paths whose [starts_at, ends_at) window overlaps the UTC window."""
    if not MANIFEST_PATH.is_file():
        return []
    start_s = starts_at.isoformat()
    end_s = ends_at.isoformat()
    paths: list[Path] = []
    try:
        with _open_ro(MANIFEST_PATH) as conn:
            rows = conn.execute(
                """
                SELECT path FROM archive_shards
                WHERE chat_id = ?
                  AND starts_at < ?
                  AND (ends_at IS NULL OR ends_at > ?)
                ORDER BY starts_at ASC, shard_id ASC
                """,
                (chat_id, end_s, start_s),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        print(f"[export_day] manifest read failed: {exc}", file=sys.stderr)
        return []
    for row in rows:
        stored = Path(row["path"])
        shard_path = stored if stored.is_absolute() else (ARCHIVE_ROOT / stored)
        if shard_path.is_file():
            paths.append(shard_path)
    return paths


def _query_day(
    shard_paths: list[Path],
    chat_id: int,
    topic_filter: set[int | None] | None,
    starts_at: datetime,
    ends_at: datetime,
) -> dict[int | None, list[sqlite3.Row]]:
    """Return messages grouped by topic_id (None = General)."""
    grouped: dict[int | None, list[sqlite3.Row]] = {}
    sql = """
        SELECT
            m.chat_id         AS chat_id,
            m.date            AS date,
            m.message_id      AS message_id,
            m.topic_id        AS topic_id,
            m.sender_id       AS sender_id,
            m.text            AS text,
            m.has_media       AS has_media,
            m.is_forum_topic_link AS is_topic_link,
            m.reply_to_msg_id AS reply_to_msg_id,
            s.display_name    AS display_name,
            s.username        AS username
        FROM archive_messages m
        LEFT JOIN archive_senders s ON s.sender_id = m.sender_id
        WHERE m.chat_id = ?
          AND m.date >= ? AND m.date < ?
        ORDER BY m.date ASC, m.message_id ASC
    """
    start_s = starts_at.isoformat()
    end_s = ends_at.isoformat()
    for shard_path in shard_paths:
        try:
            with _open_ro(shard_path) as conn:
                rows = conn.execute(sql, (chat_id, start_s, end_s)).fetchall()
        except sqlite3.DatabaseError as exc:
            print(
                f"[export_day] shard read failed: {shard_path.name}: {exc}",
                file=sys.stderr,
            )
            continue
        for row in rows:
            tid = row["topic_id"]
            if topic_filter is not None and tid not in topic_filter:
                continue
            grouped.setdefault(tid, []).append(row)
    for tid in grouped:
        grouped[tid].sort(key=lambda row: (row["date"], row["message_id"]))
    return grouped


def _format_sender(row: sqlite3.Row, context: ExportContext) -> str:
    sender_id = row["sender_id"]
    if sender_id is not None:
        sender_id = int(sender_id)
        if sender_id in context.tracked_user_ids:
            alias = context.tracked_aliases.get(sender_id)
            if alias:
                return alias
            if row["display_name"]:
                return row["display_name"]
            if row["username"]:
                return f"@{row['username']}"
            return "tracked-user"
    if row["display_name"]:
        return row["display_name"]
    if row["username"]:
        return f"@{row['username']}"
    return f"user#{row['sender_id']}"


def _format_message(row: sqlite3.Row, tz: tzinfo, context: ExportContext) -> str:
    ts = _format_local_timestamp(row["date"], tz)
    sender = _format_sender(row, context)
    media_rows = context.media_by_message.get((int(row["chat_id"]), int(row["message_id"])), [])
    media = MEDIA_MARKER if media_rows or row["has_media"] else ""
    link_marker = TOPIC_LINK_MARKER if row["is_topic_link"] else ""
    text = (row["text"] or "").strip()
    if not text and not media:
        text = "_(empty / media-only)_"
    lines = [f"- **{ts}** {sender}{media}{link_marker}", f"  {text}"]
    lines.extend(_format_media_markdown(media_row) for media_row in media_rows)
    return "\n".join(lines)


def _format_media_markdown(row: sqlite3.Row) -> str:
    file_path = Path(str(row["file_path"])).resolve()
    label = f"tracked media {row['media_index']}"
    if row["is_reply"]:
        label = f"tracked reply media {row['media_index']}"
    return f"  ![{label}]({file_path.as_posix()})"


def _format_local_timestamp(raw: str | None, tz: tzinfo) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw[:19].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _topic_label(tid: int | None) -> str:
    return "General" if tid is None else f"Topic {tid}"


def export_markdown(
    chat_id: int,
    day: datetime,
    topic_filter: set[int | None] | None,
    *,
    tz: tzinfo = ZoneInfo(DEFAULT_TIMEZONE),
    context: ExportContext | None = None,
) -> str:
    if context is None:
        context = ExportContext(
            config=None,
            target=None,
            tracked_user_ids=frozenset(),
            tracked_aliases={},
            media_by_message={},
        )
    day_start_utc, day_end_utc = _utc_day_window(day, tz)
    local_day = day.astimezone(tz).date().isoformat()
    shards = _find_shards(chat_id, day_start_utc, day_end_utc)
    if not shards:
        return (
            f"# Group {chat_id} message export\n\n"
            f"Date: {local_day} ({_timezone_label(tz)})\n"
            f"UTC window: {day_start_utc.isoformat()} -> {day_end_utc.isoformat()}\n\n"
            "_No archive shard was found for this date. Check that [full_archive] "
            "is enabled, the daemon/backfill has run, and messages exist for the day._\n"
        )

    grouped = _query_day(shards, chat_id, topic_filter, day_start_utc, day_end_utc)
    context = _with_tracked_media(context, grouped)
    total = sum(len(rows) for rows in grouped.values())

    lines: list[str] = [
        f"# Group {chat_id} message export",
        f"Date: {local_day} ({_timezone_label(tz)})",
        f"UTC window: {day_start_utc.isoformat()} -> {day_end_utc.isoformat()}",
        f"Messages: {total}",
        f"Shard: {', '.join(path.name for path in shards)}",
        "",
    ]

    if total == 0:
        lines.append("_No messages for this local day._")
        return "\n".join(lines) + "\n"

    order = sorted(grouped.keys(), key=lambda topic_id: (topic_id is None, topic_id or 0))
    for tid in order:
        rows = grouped[tid]
        lines.append(f"\n## {_topic_label(tid)} ({len(rows)})\n")
        lines.extend(_format_message(row, tz, context) for row in rows)

    return "\n".join(lines) + "\n"


def _load_export_context(config_path: Path | None, chat_id: int) -> ExportContext:
    if config_path is None:
        return ExportContext(
            config=None,
            target=None,
            tracked_user_ids=frozenset(),
            tracked_aliases={},
            media_by_message={},
        )
    config = load_config(config_path)
    target = config.target_for_chat(chat_id)
    targets = (target,) if target is not None else config.targets
    tracked_user_ids: set[int] = set()
    tracked_aliases: dict[int, str] = {}
    for item in targets:
        tracked_user_ids.update(item.tracked_user_ids)
        tracked_aliases.update(item.tracked_user_aliases)
    return ExportContext(
        config=config,
        target=target,
        tracked_user_ids=frozenset(tracked_user_ids),
        tracked_aliases=tracked_aliases,
        media_by_message={},
    )


def _with_tracked_media(
    context: ExportContext,
    grouped: dict[int | None, list[sqlite3.Row]],
) -> ExportContext:
    if context.config is None or not context.tracked_user_ids:
        return context
    keys = [
        (int(row["chat_id"]), int(row["message_id"]))
        for rows in grouped.values()
        for row in rows
        if row["sender_id"] is not None and int(row["sender_id"]) in context.tracked_user_ids
    ]
    media_by_message = _fetch_tracked_image_media(context.config.storage.db_path, keys)
    return ExportContext(
        config=context.config,
        target=context.target,
        tracked_user_ids=context.tracked_user_ids,
        tracked_aliases=context.tracked_aliases,
        media_by_message=media_by_message,
    )


def _fetch_tracked_image_media(
    db_path: Path,
    keys: list[tuple[int, int]],
) -> dict[tuple[int, int], list[sqlite3.Row]]:
    if not keys or not db_path.is_file():
        return {}
    unique_keys = list(dict.fromkeys(keys))
    media_by_message: dict[tuple[int, int], list[sqlite3.Row]] = {}
    try:
        with _open_ro(db_path) as conn:
            for chunk_start in range(0, len(unique_keys), 250):
                chunk = unique_keys[chunk_start : chunk_start + 250]
                placeholders = ",".join("(?, ?)" for _ in chunk)
                params: list[object] = []
                for chat_id, message_id in chunk:
                    params.extend([chat_id, message_id])
                rows = conn.execute(
                    f"""
                    SELECT chat_id, message_id, media_index, file_path, mime_type,
                           file_size, is_reply
                    FROM media
                    WHERE (chat_id, message_id) IN (VALUES {placeholders})
                    ORDER BY chat_id ASC, message_id ASC, media_index ASC
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    mime_type = row["mime_type"]
                    if mime_type is not None and not str(mime_type).startswith("image/"):
                        continue
                    file_path = Path(str(row["file_path"]))
                    if not file_path.is_file():
                        continue
                    key = (int(row["chat_id"]), int(row["message_id"]))
                    media_by_message.setdefault(key, []).append(row)
    except sqlite3.DatabaseError as exc:
        print(f"[export_day] tracked media read failed: {exc}", file=sys.stderr)
        return {}
    return media_by_message


def _parse_day(raw: str | None, tz: tzinfo) -> datetime:
    if raw is None:
        return (datetime.now(tz) - timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"--day must be YYYY-MM-DD, parse failed: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _parse_timezone(raw: str) -> ZoneInfo:
    try:
        return ZoneInfo(raw)
    except Exception as exc:
        raise SystemExit(f"--timezone must be a valid IANA timezone, got {raw!r}") from exc


def _timezone_label(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export one local calendar day from full_archive as Markdown for AI analysis."
    )
    parser.add_argument("--chat", type=int, required=True, help="Target group chat_id")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config.toml",
        help="Config path for tracked user aliases and downloaded tracked media.",
    )
    parser.add_argument(
        "--day",
        type=str,
        default=None,
        help=f"Local date, YYYY-MM-DD. Default = yesterday in {DEFAULT_TIMEZONE}.",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default=DEFAULT_TIMEZONE,
        help=f"IANA timezone for --day boundaries. Default = {DEFAULT_TIMEZONE}.",
    )
    parser.add_argument(
        "--topics",
        type=int,
        nargs="*",
        default=None,
        help="Optional topic_id filter. Omit for the whole group.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Output file path. Default stdout.")
    args = parser.parse_args(argv)

    tz = _parse_timezone(args.timezone)
    day = _parse_day(args.day, tz)
    context = _load_export_context(args.config, args.chat)
    topic_filter: set[int | None] | None = None
    if args.topics is not None:
        topic_filter = set(args.topics)

    md = export_markdown(args.chat, day, topic_filter, tz=tz, context=context)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"[export_day] wrote {args.out} ({len(md)} chars)")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
