from __future__ import annotations

import asyncio
import sqlite3
import tomllib
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import telegram_watch.cli as cli_module
from telegram_watch import full_archive_storage as archive_storage
from telegram_watch.full_archive_storage import ArchiveContextResult, ArchiveMedia
from telegram_watch import storage as tracked_storage
from telegram_watch.cli import (
    _confirm_retention,
    _run_archive_backfill_command,
    _run_archive_context_command,
    _run_archive_qa_init_command,
    _run_archive_repair_command,
    _run_archive_senders_backfill_command,
    _run_archive_status_command,
    _run_list_topics_command,
    build_parser,
)
from telegram_watch.config import load_config


def test_confirm_retention_auto_confirm_bypasses_prompt() -> None:
    assert _confirm_retention(365, auto_confirm=True) is True


def test_cleanup_replies_parser_defaults() -> None:
    parser = build_parser()
    assert parser.prog == "tgwatch"
    args = parser.parse_args(["cleanup-replies", "--config", "config.toml"])
    assert args.command == "cleanup-replies"
    assert args.apply is False
    assert args.no_backup is False


def test_archive_backfill_parser_defaults_to_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(["archive-backfill", "--config", "config.toml"])
    assert args.command == "archive-backfill"
    assert args.limit is None
    assert args.apply is False
    assert args.dry_run is False


def test_archive_backfill_parser_accepts_explicit_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["archive-backfill", "--config", "config.toml", "--limit", "100", "--dry-run"]
    )
    assert args.command == "archive-backfill"
    assert args.limit == 100
    assert args.apply is False
    assert args.dry_run is True


def test_archive_backfill_help_explains_zero_limit_noop(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["archive-backfill", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("archive-backfill --help should exit")

    output = capsys.readouterr().out
    assert "--limit" in output
    assert "Non-negative maximum messages" in output
    assert "0 is a no-op" in output
    assert "does not connect to Telegram" in output


def test_archive_senders_backfill_parser_defaults_to_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["archive-senders-backfill", "--config", "config.toml"]
    )

    assert args.command == "archive-senders-backfill"
    assert args.limit is None
    assert args.apply is False
    assert args.dry_run is False


def test_archive_senders_backfill_help_explains_distinct_sender_limit(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["archive-senders-backfill", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("archive-senders-backfill --help should exit")

    output = capsys.readouterr().out
    assert "distinct senders" in output
    assert "0 is" in output
    assert "a no-op" in output
    assert "does not connect to Telegram" in output


def test_archive_status_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(["archive-status", "--config", "config.toml"])
    assert args.command == "archive-status"


def test_archive_status_returns_nonzero_for_degraded_report_without_errors(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    def fake_status(_root_dir, *, tracked_db_path=None):
        return archive_storage.ArchiveStatusReport(
            root_dir=config.full_archive.root_dir,
            manifest_path=config.full_archive.root_dir / "manifest.sqlite3",
            manifest_exists=True,
            shard_count=1,
            missing_shard_count=1,
            manifest_message_count=1,
            actual_message_count=0,
            archive_row_count=0,
            tracked_ref_count=0,
            link_count=0,
            media_metadata_count=0,
            tracked_db_link_count=0,
            file_size_bytes=0,
            missing_index_count=0,
            missing_schema_table_count=0,
            errors=(),
            shards=(),
            current_tracked_db_linked=False,
            current_tracked_db_readable=True,
        )

    monkeypatch.setattr(cli_module, "inspect_archive_status", fake_status)

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "Errors:" not in output


def test_archive_repair_parser_defaults_to_dry_run() -> None:
    parser = build_parser()
    args = parser.parse_args(["archive-repair", "--config", "config.toml"])
    assert args.command == "archive-repair"
    assert args.apply is False
    assert args.dry_run is False
    assert args.prune_missing_shards is False


def test_archive_repair_parser_accepts_prune_missing_shards() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "archive-repair",
            "--config",
            "config.toml",
            "--prune-missing-shards",
        ]
    )
    assert args.command == "archive-repair"
    assert args.prune_missing_shards is True


def test_archive_repair_help_explains_prune_dry_run_boundary(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["archive-repair", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("archive-repair --help should exit")

    output = capsys.readouterr().out
    assert "--prune-missing-shards" in output
    assert "dry-run by default" in output
    assert "writes only with --apply" in output
    assert "never deletes files" in output


def test_archive_context_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "archive-context",
            "--config",
            "config.toml",
            "--chat",
            "-100123",
            "--message-id",
            "456",
            "--before-minutes",
            "15",
            "--after-minutes",
            "3",
            "--topic-id",
            "99",
        ]
    )
    assert args.command == "archive-context"
    assert args.chat == -100123
    assert args.message_id == 456
    assert args.before_minutes == 15
    assert args.after_minutes == 3
    assert args.topic_id == 99


def test_archive_context_help_explains_general_topic_boundary(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["archive-context", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("archive-context --help should exit")

    output = capsys.readouterr().out
    assert "--topic-id" in output
    assert "must be > 1" in output
    assert "General topic 1" in output
    assert "whole-group context" in output


def test_format_context_row_includes_topic_id_and_null_marker() -> None:
    topic_row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=1,
        topic_id=99,
        sender_id=123,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        text="topic row",
        effective_text="topic row",
        payload_mode="archive",
        tracked_text=None,
        tracked_replied_text=None,
        tracked_row_found=False,
        tracked_db_matches_current=True,
        tracked_message_chat_id=None,
        tracked_message_id=None,
        reply_to_msg_id=88,
        reply_to_top_id=99,
    )
    null_topic_row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=2,
        topic_id=None,
        sender_id=123,
        date=datetime(2026, 5, 1, 12, 1, tzinfo=timezone.utc),
        text="general row",
        effective_text="general row",
        payload_mode="archive",
        tracked_text=None,
        tracked_replied_text=None,
        tracked_row_found=False,
        tracked_db_matches_current=True,
        tracked_message_chat_id=None,
        tracked_message_id=None,
    )

    formatted_topic = cli_module._format_context_row(
        topic_row,
        target_chat_id=-1001,
        target_message_id=1,
    )
    formatted_null_topic = cli_module._format_context_row(null_topic_row)

    assert "*" in formatted_topic
    assert "99" in formatted_topic
    assert "reply:88,top:99" in formatted_topic
    assert "1         \n  Text: topic row" in formatted_topic
    assert "-                        2         \n  Text: general row" in formatted_null_topic


def test_format_context_row_keeps_long_text_out_of_metadata_line() -> None:
    row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=1,
        topic_id=99,
        sender_id=123,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        text="unused",
        effective_text=" ".join(["verylongtext"] * 40),
        payload_mode="archive",
        tracked_text=None,
        tracked_replied_text=None,
        tracked_row_found=False,
        tracked_db_matches_current=True,
        tracked_message_chat_id=None,
        tracked_message_id=None,
        media=(
            ArchiveMedia(
                media_index=0,
                media_kind="MessageMediaDocument",
                mime_type="application/pdf",
                file_size=123456,
                file_name="very-long-file-name-" * 12 + ".pdf",
            ),
        ),
    )

    formatted = cli_module._format_context_row(row)
    metadata, text_line = formatted.split("\n", 1)

    assert "verylongtext" not in metadata
    assert "very-long-file-name" not in metadata
    assert text_line.startswith("  Text: ")
    assert "…" in text_line


def test_format_context_row_prints_tracked_reply_snapshot() -> None:
    row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=1,
        topic_id=99,
        sender_id=123,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        text=None,
        effective_text="tracked answer",
        payload_mode="tracked_ref",
        tracked_text="tracked answer",
        tracked_replied_text="reply snapshot text",
        tracked_row_found=True,
        tracked_db_matches_current=True,
        tracked_message_chat_id=-1001,
        tracked_message_id=1,
    )

    formatted = cli_module._format_context_row(row)

    assert "  Text: tracked answer" in formatted
    assert "  Reply snapshot: reply snapshot text" in formatted


def test_format_context_row_ignores_archive_reply_snapshot() -> None:
    row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=1,
        topic_id=99,
        sender_id=123,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        text="archive row",
        effective_text="archive row",
        payload_mode="archive",
        tracked_text=None,
        tracked_replied_text="unexpected reply snapshot",
        tracked_row_found=False,
        tracked_db_matches_current=True,
        tracked_message_chat_id=None,
        tracked_message_id=None,
    )

    formatted = cli_module._format_context_row(row)

    assert "  Text: archive row" in formatted
    assert "Reply snapshot:" not in formatted


def test_format_context_row_prefers_alias_and_never_prints_raw_sender_id() -> None:
    sender_id = 987654321
    row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=1,
        topic_id=None,
        sender_id=sender_id,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        text="archive row",
        effective_text="archive row",
        payload_mode="archive",
        tracked_text=None,
        tracked_replied_text=None,
        tracked_row_found=False,
        tracked_db_matches_current=True,
        tracked_message_chat_id=None,
        tracked_message_id=None,
        sender=archive_storage.ArchiveSender(
            sender_id=sender_id,
            username="archive_user",
            display_name="Archive User",
            first_seen_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        ),
    )

    alias_output = cli_module._format_context_row(row, sender_alias="Core Tracker")
    snapshot_output = cli_module._format_context_row(row)

    assert "Core Tracker" in alias_output
    assert "Archive User (@archive_user)" not in alias_output
    assert "Archive User (@archive_user)" in snapshot_output
    assert str(sender_id) not in alias_output
    assert str(sender_id) not in snapshot_output


def test_format_context_row_uses_anonymous_label_without_sender_snapshot() -> None:
    sender_id = 987654321
    row = cli_module.ArchiveContextMessage(
        chat_id=-1001,
        message_id=1,
        topic_id=None,
        sender_id=sender_id,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        text="archive row",
        effective_text="archive row",
        payload_mode="archive",
        tracked_text=None,
        tracked_replied_text=None,
        tracked_row_found=False,
        tracked_db_matches_current=True,
        tracked_message_chat_id=None,
        tracked_message_id=None,
    )

    output = cli_module._format_context_row(row)

    assert "Anonymous sender" in output
    assert str(sender_id) not in output


def test_archive_status_disabled_does_not_create_archive_root(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert _run_archive_status_command(config) == 0
    assert not config.full_archive.root_dir.exists()


def test_archive_status_enabled_without_manifest_is_empty_and_readonly(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert _run_archive_status_command(config) == 0

    output = capsys.readouterr().out
    assert "Status: empty" in output
    assert "Manifest exists: False" in output
    assert "Status: degraded" not in output
    assert not config.full_archive.root_dir.exists()


def test_archive_status_degrades_orphaned_shards_without_manifest(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    orphaned_shard = (
        config.full_archive.root_dir
        / "shards"
        / "group_-1001"
        / "2026-05.sqlite3"
    )
    orphaned_shard.parent.mkdir(parents=True)
    sqlite3.connect(orphaned_shard).close()

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "Manifest exists: False" in output
    assert "archive root has shard file(s) but no manifest (count=1)" in output
    assert str(orphaned_shard) not in output


def test_archive_status_degrades_unregistered_shards_with_manifest(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    registered = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(registered.path)
    archive_storage.ensure_shard_schema(shard)
    shard.close()
    manifest.close()
    unregistered_shard = (
        config.full_archive.root_dir
        / "shards"
        / "group_-1001"
        / "2026-05-999.sqlite3"
    )
    sqlite3.connect(unregistered_shard).close()

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "archive root has unregistered shard file(s) (count=1)" in output
    assert str(unregistered_shard) not in output


def test_archive_status_prints_current_tracked_db_health(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    tracked = tracked_storage.connect(config.storage.db_path)
    tracked_storage.ensure_schema(tracked)
    tracked.close()
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    archive_storage.record_tracked_db_link(manifest, config.storage.db_path)
    manifest.close()

    assert _run_archive_status_command(config) == 0

    output = capsys.readouterr().out
    assert "Current tracked DB linked: yes" in output
    assert "Current tracked DB readable: yes" in output
    assert str(config.storage.db_path) not in output


def test_archive_status_returns_nonzero_for_missing_manifest_schema_table(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest_path = config.full_archive.root_dir / "manifest.sqlite3"
    manifest_path.parent.mkdir(parents=True)
    manifest = sqlite3.connect(manifest_path)
    try:
        manifest.execute(
            """
            CREATE TABLE archive_shards (
                shard_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER,
                path TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        manifest.commit()
    finally:
        manifest.close()

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "Missing schema tables: 1" in output
    assert "manifest: missing schema table(s): tracked_db_links" in output


def test_archive_status_reports_archive_messages_without_tracked_db_link(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    tracked = tracked_storage.connect(config.storage.db_path)
    tracked_storage.ensure_schema(tracked)
    tracked.close()
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)
    archive_storage.persist_archive_message(
        shard,
        archive_storage.ArchiveMessage(
            chat_id=-1001,
            message_id=1,
            topic_id=None,
            sender_id=456,
            date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            text="context",
            raw_text="context",
            message_kind="message",
            reply_to_msg_id=None,
            reply_to_top_id=None,
            is_forum_topic_link=False,
            has_media=False,
        ),
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    shard.close()
    manifest.close()

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "Tracked DB links: 0" in output
    assert "Current tracked DB linked: no" in output
    assert "Current tracked DB readable: yes" in output
    assert "archive manifest has messages but no tracked DB link" in output
    assert str(config.storage.db_path) not in output


def test_archive_status_returns_nonzero_for_incomplete_tracked_ref_metadata(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    tracked = tracked_storage.connect(config.storage.db_path)
    try:
        tracked_storage.ensure_schema(tracked)
        tracked_storage.persist_message(
            tracked,
            tracked_storage.StoredMessage(
                chat_id=-1001,
                message_id=1,
                sender_id=123,
                date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                text="tracked",
                reply_to_msg_id=None,
                replied_sender_id=None,
                replied_date=None,
                replied_text=None,
            ),
            [],
        )
    finally:
        tracked.close()
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)
    try:
        archive_storage.persist_archive_message(
            shard,
            archive_storage.ArchiveMessage(
                chat_id=-1001,
                message_id=1,
                topic_id=None,
                sender_id=123,
                date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                text="duplicate",
                raw_text="duplicate",
                message_kind="message",
                reply_to_msg_id=None,
                reply_to_top_id=None,
                is_forum_topic_link=False,
                has_media=False,
            ),
            tracked_db_path=config.storage.db_path,
            archive_root_dir=config.full_archive.root_dir,
        )
        shard.execute(
            """
            UPDATE archive_messages
            SET tracked_db_path = NULL
            WHERE chat_id = ? AND message_id = ?
            """,
            (-1001, 1),
        )
        shard.execute(
            "DELETE FROM archive_tracked_links WHERE chat_id = ? AND message_id = ?",
            (-1001, 1),
        )
        shard.commit()
    finally:
        shard.close()
    archive_storage.record_tracked_db_link(
        manifest,
        config.storage.db_path,
        archive_root_dir=config.full_archive.root_dir,
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    manifest.close()

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "Tracked refs: 1" in output
    assert "Tracked links: 0" in output
    assert "incomplete tracked_ref metadata (rows=1)" in output
    assert "tracked_ref/link count mismatch" in output
    assert str(config.storage.db_path) not in output


def test_archive_status_returns_nonzero_for_stale_tracked_ref_media_rows(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    tracked = tracked_storage.connect(config.storage.db_path)
    try:
        tracked_storage.ensure_schema(tracked)
        tracked_storage.persist_message(
            tracked,
            tracked_storage.StoredMessage(
                chat_id=-1001,
                message_id=1,
                sender_id=123,
                date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                text="tracked",
                reply_to_msg_id=None,
                replied_sender_id=None,
                replied_date=None,
                replied_text=None,
            ),
            [],
        )
    finally:
        tracked.close()
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)
    try:
        archive_storage.persist_archive_message(
            shard,
            archive_storage.ArchiveMessage(
                chat_id=-1001,
                message_id=1,
                topic_id=None,
                sender_id=123,
                date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                text="duplicate",
                raw_text="duplicate",
                message_kind="message",
                reply_to_msg_id=None,
                reply_to_top_id=None,
                is_forum_topic_link=False,
                has_media=False,
            ),
            tracked_db_path=config.storage.db_path,
            archive_root_dir=config.full_archive.root_dir,
        )
        shard.execute(
            """
            UPDATE archive_messages
            SET text = ?, raw_text = ?
            WHERE chat_id = ? AND message_id = ?
            """,
            ("stale text", "stale raw text", -1001, 1),
        )
        shard.execute(
            """
            INSERT INTO archive_media (
                chat_id, message_id, media_index, media_kind, mime_type,
                file_size, file_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                -1001,
                1,
                0,
                "MessageMediaPhoto",
                "image/jpeg",
                123,
                "stale.jpg",
                "2026-05-01T12:00:00+00:00",
                "2026-05-01T12:00:00+00:00",
            ),
        )
        shard.commit()
    finally:
        shard.close()
    archive_storage.record_tracked_db_link(
        manifest,
        config.storage.db_path,
        archive_root_dir=config.full_archive.root_dir,
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    manifest.close()

    assert _run_archive_status_command(config) == 1

    output = capsys.readouterr().out
    assert "Status: degraded" in output
    assert "Archive media metadata rows: 1" in output
    assert "tracked_ref archive text payload should be cleared" in output
    assert "tracked_ref archive media metadata should be removed" in output
    assert "rows=1" in output
    assert str(config.storage.db_path) not in output


def test_archive_backfill_zero_limit_does_not_create_archive_root(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        backfill_limit_messages = 0
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert asyncio.run(_run_archive_backfill_command(config, limit=None, apply=False)) == 0
    assert not config.full_archive.root_dir.exists()


def test_archive_senders_backfill_prints_aggregate_results_only(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fake_backfill(_config, *, limit=None, apply=False):
        assert limit == 10
        assert apply is False
        return SimpleNamespace(
            candidates=7,
            schema_updates=0,
            reused=1,
            cached=3,
            fetched=2,
            unresolved=1,
            written_senders=6,
            shard_writes=8,
            dry_run=True,
        )

    monkeypatch.setattr(cli_module, "run_archive_senders_backfill", fake_backfill)

    result = asyncio.run(
        _run_archive_senders_backfill_command(
            config,
            limit=10,
            apply=False,
        )
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "Missing sender snapshots: 7" in output
    assert "Sender schema updates: 0" in output
    assert "Resolved from session cache: 3" in output
    assert "Resolved from Telegram history: 2" in output
    assert "Unresolved senders: 1" in output
    assert "Dry-run only" in output
    assert "sender_id" not in output


def test_archive_senders_backfill_apply_allows_missing_sender_schema_only(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config = _load_full_archive_test_config(tmp_path)
    _create_archive_with_missing_additive_table(config, "archive_senders")

    async def fake_backfill(_config, *, limit=None, apply=False):
        assert limit == 10
        assert apply is True
        return SimpleNamespace(
            candidates=1,
            schema_updates=1,
            reused=0,
            cached=1,
            fetched=0,
            unresolved=0,
            written_senders=1,
            shard_writes=1,
            dry_run=False,
        )

    monkeypatch.setattr(cli_module, "run_archive_senders_backfill", fake_backfill)

    result = asyncio.run(
        _run_archive_senders_backfill_command(
            config,
            limit=10,
            apply=True,
        )
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "Sender schema updates: 1" in output
    assert "archive health is degraded" not in output


def test_archive_senders_backfill_apply_rejects_other_archive_degradation(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config = _load_full_archive_test_config(tmp_path)
    _create_archive_with_missing_additive_table(config, "archive_media")

    async def fail_backfill(*_args, **_kwargs):
        raise AssertionError("sender backfill must not bypass unrelated degradation")

    monkeypatch.setattr(cli_module, "run_archive_senders_backfill", fail_backfill)

    result = asyncio.run(
        _run_archive_senders_backfill_command(
            config,
            limit=10,
            apply=True,
        )
    )

    assert result == 2
    output = capsys.readouterr().out
    assert "Archive sender backfill error" in output
    assert "archive health is degraded" in output
    assert "missing schema table(s): archive_media" in output


def test_archive_backfill_prints_updated_rows(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fake_backfill(_config, *, limit=None, apply=False):
        return SimpleNamespace(
            scanned=1,
            matched=1,
            skipped_scope=0,
            skipped_invalid=0,
            archived=0,
            linked=0,
            updated=1,
            dry_run=False,
        )

    monkeypatch.setattr(cli_module, "run_archive_backfill", fake_backfill)

    assert asyncio.run(_run_archive_backfill_command(config, limit=1, apply=True)) == 0

    output = capsys.readouterr().out
    assert "Archived rows: 0" in output
    assert "Tracked links: 0" in output
    assert "Updated rows: 1" in output


def test_archive_backfill_apply_refuses_degraded_archive_before_backfill(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    _create_degraded_archive_without_tracked_link(config)

    async def fail_backfill(*_args, **_kwargs):
        raise AssertionError("archive backfill should not run on degraded archive")

    monkeypatch.setattr(cli_module, "run_archive_backfill", fail_backfill)

    result = asyncio.run(_run_archive_backfill_command(config, limit=1, apply=True))

    assert result == 2
    output = capsys.readouterr().out
    assert "archive health is degraded" in output
    assert "archive-status" in output
    assert "archive-repair --dry-run" in output
    assert "Archive health errors:" in output
    assert str(config.storage.db_path) not in output


def test_archive_backfill_apply_zero_limit_skips_degraded_preflight(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    _create_degraded_archive_without_tracked_link(config)

    assert asyncio.run(_run_archive_backfill_command(config, limit=0, apply=True)) == 0

    output = capsys.readouterr().out
    assert "Scanned: 0" in output
    assert "archive health is degraded" not in output


def test_archive_backfill_negative_limit_skips_health_preflight(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    def fail_preflight(_config):
        raise AssertionError("health preflight should not run for invalid limit")

    monkeypatch.setattr(cli_module, "_archive_backfill_apply_preflight", fail_preflight)

    try:
        asyncio.run(_run_archive_backfill_command(config, limit=-1, apply=True))
    except SystemExit as exc:
        exit_code = exc.code
    else:
        raise AssertionError("negative archive-backfill limit should exit")

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "archive-backfill limit must be >= 0" in output
    assert "archive health is degraded" not in output
    assert not config.full_archive.root_dir.exists()


def test_archive_backfill_apply_allows_empty_archive_preflight(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fake_backfill(_config, *, limit=None, apply=False):
        assert limit == 1
        assert apply is True
        assert not config.full_archive.root_dir.exists()
        return SimpleNamespace(
            scanned=0,
            matched=0,
            skipped_scope=0,
            skipped_invalid=0,
            archived=0,
            linked=0,
            updated=0,
            dry_run=False,
        )

    monkeypatch.setattr(cli_module, "run_archive_backfill", fake_backfill)

    assert asyncio.run(_run_archive_backfill_command(config, limit=1, apply=True)) == 0
    output = capsys.readouterr().out
    assert "Scanned: 0" in output
    assert not config.full_archive.root_dir.exists()


def _create_degraded_archive_without_tracked_link(config) -> None:
    tracked = tracked_storage.connect(config.storage.db_path)
    try:
        tracked_storage.ensure_schema(tracked)
    finally:
        tracked.close()

    message_date = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=message_date,
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)
    try:
        archive_storage.persist_archive_message(
            shard,
            archive_storage.ArchiveMessage(
                chat_id=-1001,
                message_id=1,
                topic_id=None,
                sender_id=123,
                date=message_date,
                text="context",
                raw_text="context",
                message_kind="message",
                reply_to_msg_id=None,
                reply_to_top_id=None,
                is_forum_topic_link=False,
                has_media=False,
            ),
        )
    finally:
        shard.close()
    archive_storage.record_shard_write(manifest, shard_meta)
    manifest.close()


def _load_full_archive_test_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    return load_config(config_path)


def _create_archive_with_missing_additive_table(config, table_name: str) -> None:
    tracked = tracked_storage.connect(config.storage.db_path)
    try:
        tracked_storage.ensure_schema(tracked)
    finally:
        tracked.close()

    message_date = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    try:
        shard_meta = archive_storage.select_shard(
            manifest,
            config.full_archive.root_dir,
            chat_id=-1001,
            message_date=message_date,
            max_messages_per_shard=500_000,
            max_shard_size_bytes=1024 * 1024,
        )
        shard = archive_storage.connect(shard_meta.path)
        try:
            archive_storage.persist_archive_message(
                shard,
                archive_storage.ArchiveMessage(
                    chat_id=-1001,
                    message_id=1,
                    topic_id=None,
                    sender_id=456,
                    date=message_date,
                    text="context",
                    raw_text="context",
                    message_kind="message",
                    reply_to_msg_id=None,
                    reply_to_top_id=None,
                    is_forum_topic_link=False,
                    has_media=False,
                ),
            )
            shard.execute(f"DROP TABLE {table_name}")
            shard.commit()
        finally:
            shard.close()
        archive_storage.record_shard_write(manifest, shard_meta)
        archive_storage.record_tracked_db_link(
            manifest,
            config.storage.db_path,
            archive_root_dir=config.full_archive.root_dir,
        )
    finally:
        manifest.close()


def test_archive_backfill_command_rejects_apply_with_dry_run(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fail_backfill(*_args, **_kwargs):
        raise AssertionError("archive backfill should not run for conflicting modes")

    monkeypatch.setattr(cli_module, "run_archive_backfill", fail_backfill)

    result = asyncio.run(
        _run_archive_backfill_command(config, limit=1, apply=True, dry_run=True)
    )

    assert result == 2
    output = capsys.readouterr().out
    assert "--apply and --dry-run cannot be used together" in output


def test_archive_backfill_disabled_is_readable_and_readonly(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    try:
        asyncio.run(_run_archive_backfill_command(config, limit=1, apply=True))
    except SystemExit as exc:
        exit_code = exc.code
    else:
        raise AssertionError("disabled archive-backfill should exit")

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "Archive backfill error" in output
    assert "full_archive must be enabled" in output
    assert "Traceback" not in output
    assert not config.full_archive.root_dir.exists()


def test_archive_backfill_negative_limit_is_readable_and_readonly(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    try:
        asyncio.run(_run_archive_backfill_command(config, limit=-1, apply=True))
    except SystemExit as exc:
        exit_code = exc.code
    else:
        raise AssertionError("negative archive-backfill limit should exit")

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "Archive backfill error" in output
    assert "archive-backfill limit must be >= 0" in output
    assert "Traceback" not in output
    assert not config.full_archive.root_dir.exists()


def test_archive_context_disabled_does_not_create_archive_root(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert (
        _run_archive_context_command(
            config,
            chat_id=-1001,
            message_id=1,
            before_minutes=10,
            after_minutes=5,
            topic_id=None,
        )
        == 2
    )
    assert not config.full_archive.root_dir.exists()


def test_archive_context_rejects_invalid_window_without_creating_archive_root(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert (
        _run_archive_context_command(
            config,
            chat_id=-1001,
            message_id=1,
            before_minutes=-1,
            after_minutes=5,
            topic_id=None,
        )
        == 2
    )

    output = capsys.readouterr().out
    assert "before/after minutes must be >= 0" in output
    assert not config.full_archive.root_dir.exists()


def test_archive_context_rejects_invalid_topic_id_without_creating_archive_root(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    for topic_id in (0, 1, -1):
        assert (
            _run_archive_context_command(
                config,
                chat_id=-1001,
                message_id=1,
                before_minutes=10,
                after_minutes=5,
                topic_id=topic_id,
            )
            == 2
        )

    output = capsys.readouterr().out
    assert output.count("topic_id must be a Telegram forum topic ID > 1") == 3
    assert not config.full_archive.root_dir.exists()


def test_archive_context_reports_missing_tracked_db_before_target_lookup(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert (
        _run_archive_context_command(
            config,
            chat_id=-1001,
            message_id=1,
            before_minutes=10,
            after_minutes=5,
            topic_id=None,
        )
        == 2
    )

    output = capsys.readouterr().out
    normalized = " ".join(output.split())
    assert "cannot read tracked DB for target message" in normalized
    assert "missing tracked DB" in normalized
    assert "tracked message not found" not in normalized
    assert str(config.storage.db_path) not in normalized
    assert not config.storage.db_path.exists()
    assert not config.full_archive.root_dir.exists()


def test_archive_context_reports_tracked_db_missing_target_date_column(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    config.storage.db_path.parent.mkdir(parents=True)
    tracked = sqlite3.connect(config.storage.db_path)
    try:
        tracked.execute("CREATE TABLE messages (chat_id INTEGER, message_id INTEGER)")
        tracked.commit()
    finally:
        tracked.close()

    assert (
        _run_archive_context_command(
            config,
            chat_id=-1001,
            message_id=1,
            before_minutes=10,
            after_minutes=5,
            topic_id=None,
        )
        == 2
    )

    output = capsys.readouterr().out
    normalized = " ".join(output.split())
    assert "cannot read tracked DB for target message" in normalized
    assert "tracked DB messages table missing column(s): date" in normalized
    assert str(config.storage.db_path) not in normalized
    assert not config.full_archive.root_dir.exists()


def test_archive_context_after_deleted_archive_root_is_empty_and_readonly(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    message_date = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    tracked = tracked_storage.connect(config.storage.db_path)
    try:
        tracked_storage.ensure_schema(tracked)
        tracked_storage.persist_message(
            tracked,
            tracked_storage.StoredMessage(
                chat_id=-1001,
                message_id=1,
                sender_id=123,
                date=message_date,
                text="tracked payload",
                reply_to_msg_id=None,
                replied_sender_id=None,
                replied_date=None,
                replied_text=None,
            ),
            [],
        )
    finally:
        tracked.close()

    assert (
        _run_archive_context_command(
            config,
            chat_id=-1001,
            message_id=1,
            before_minutes=10,
            after_minutes=5,
            topic_id=None,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Tracked message time: 2026-05-01T12:00:00+00:00" in output
    assert "No archived context rows found." in output
    assert "Target archived row: no" in output
    assert not config.full_archive.root_dir.exists()


def test_archive_context_returns_nonzero_for_missing_context_shard(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    tracked = tracked_storage.connect(config.storage.db_path)
    tracked_storage.ensure_schema(tracked)
    tracked_storage.persist_message(
        tracked,
        tracked_storage.StoredMessage(
            chat_id=-1001,
            message_id=1,
            sender_id=123,
            date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            text="tracked",
            reply_to_msg_id=None,
            replied_sender_id=None,
            replied_date=None,
            replied_text=None,
        ),
        [],
    )
    tracked.close()
    manifest = config.full_archive.root_dir / "manifest.sqlite3"
    manifest.parent.mkdir(parents=True)
    conn = sqlite3.connect(manifest)
    try:
        conn.execute(
            """
            CREATE TABLE archive_shards (
                shard_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER,
                path TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO archive_shards (
                shard_id, chat_id, topic_id, path, starts_at, ends_at,
                message_count, file_size_bytes, status, created_at, closed_at
            ) VALUES (?, ?, NULL, ?, ?, ?, 0, 0, 'active', ?, NULL)
            """,
            (
                "-1001:2026-05:001",
                -1001,
                str(config.full_archive.root_dir / "shards/group_-1001/2026-05.sqlite3"),
                "2026-05-01T00:00:00+00:00",
                "2026-06-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    assert (
        _run_archive_context_command(
            config,
            chat_id=-1001,
            message_id=1,
            before_minutes=10,
            after_minutes=5,
            topic_id=None,
        )
        == 1
    )


def test_archive_context_prints_context_errors(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr(
        cli_module,
        "find_tracked_message_date",
        lambda *_args, **_kwargs: datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        cli_module,
        "tracked_message_date_lookup_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_context_result",
        lambda *_args, **_kwargs: ArchiveContextResult(
            messages=(),
            skipped_shards=(),
            errors=("manifest schema unreadable: no such table: archive_shards",),
        ),
    )

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=1,
        before_minutes=10,
        after_minutes=5,
        topic_id=None,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Errors:" in out
    assert "manifest schema unreadable" in out


def test_archive_context_returns_nonzero_for_unresolved_tracked_ref(
    monkeypatch, tmp_path, capsys
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr(
        cli_module,
        "find_tracked_message_date",
        lambda *_args, **_kwargs: datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        cli_module,
        "tracked_message_date_lookup_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_context_result",
        lambda *_args, **_kwargs: ArchiveContextResult(
            messages=(
                cli_module.ArchiveContextMessage(
                    chat_id=-1001,
                    message_id=1,
                    topic_id=None,
                    sender_id=123,
                    date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    text=None,
                    effective_text=None,
                    payload_mode="tracked_ref",
                    tracked_text=None,
                    tracked_replied_text=None,
                    tracked_row_found=False,
                    tracked_db_matches_current=True,
                    tracked_message_chat_id=-1001,
                    tracked_message_id=1,
                ),
            ),
            skipped_shards=(),
            errors=(
                "-1001:2026-05:001: 1 tracked_ref row(s) could not resolve tracked DB row",
            ),
        ),
    )

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=1,
        before_minutes=10,
        after_minutes=5,
        topic_id=None,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "tracked_ref row(s) could not resolve tracked DB row" in out


def test_archive_context_prints_media_metadata(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr(
        cli_module,
        "find_tracked_message_date",
        lambda *_args, **_kwargs: datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        cli_module,
        "tracked_message_date_lookup_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_context_result",
        lambda *_args, **_kwargs: ArchiveContextResult(
            messages=(
                cli_module.ArchiveContextMessage(
                    chat_id=-1001,
                    message_id=1,
                    topic_id=None,
                    sender_id=456,
                    date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    text="chart",
                    effective_text="chart",
                    payload_mode="archive",
                    tracked_text=None,
                    tracked_replied_text=None,
                    tracked_row_found=False,
                    tracked_db_matches_current=True,
                    tracked_message_chat_id=None,
                    tracked_message_id=None,
                    media=(
                        ArchiveMedia(
                            media_index=0,
                            media_kind="MessageMediaPhoto",
                            mime_type="image/jpeg",
                            file_size=12345,
                            file_name="chart.jpg",
                        ),
                    ),
                ),
            ),
            skipped_shards=(),
            errors=(),
        ),
    )

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=1,
        before_minutes=10,
        after_minutes=5,
        topic_id=None,
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Tracked message time: 2026-05-01T12:00:00+00:00" in out
    assert "Context window: 2026-05-01T11:50:00+00:00 -> 2026-05-01T12:05:00+00:00" in out
    assert "Context filters: before 10m, after 5m, topic whole group" in out
    assert "Target archived row: yes" in out
    assert "Target" in out
    assert "Topic" in out
    assert "Reply" in out
    assert "Text: chart" in out
    assert "(media:MessageMediaPhoto/image/jpeg/12345B/chart.jpg)" in out


def test_archive_context_reports_when_target_row_is_missing_from_archive(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr(
        cli_module,
        "find_tracked_message_date",
        lambda *_args, **_kwargs: datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        cli_module,
        "tracked_message_date_lookup_error",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_context_result",
        lambda *_args, **_kwargs: ArchiveContextResult(
            messages=(
                cli_module.ArchiveContextMessage(
                    chat_id=-1001,
                    message_id=2,
                    topic_id=None,
                    sender_id=456,
                    date=datetime(2026, 5, 1, 12, 1, tzinfo=timezone.utc),
                    text="nearby",
                    effective_text="nearby",
                    payload_mode="archive",
                    tracked_text=None,
                    tracked_replied_text=None,
                    tracked_row_found=False,
                    tracked_db_matches_current=True,
                    tracked_message_chat_id=None,
                    tracked_message_id=None,
                ),
            ),
            skipped_shards=(),
            errors=(),
        ),
    )

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=1,
        before_minutes=10,
        after_minutes=5,
        topic_id=99,
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "topic 99" in out
    assert "Target archived row: no" in out
    assert "nearby" in out


def test_archive_context_reads_real_tracked_and_archive_databases(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    tracked = tracked_storage.connect(config.storage.db_path)
    tracked_storage.ensure_schema(tracked)
    tracked_storage.persist_message(
        tracked,
        tracked_storage.StoredMessage(
            chat_id=-1001,
            message_id=10,
            sender_id=123,
            date=base,
            text="tracked target text",
            reply_to_msg_id=9,
            replied_sender_id=456,
            replied_date=base,
            replied_text="tracked reply snapshot",
        ),
        [],
    )
    tracked.close()

    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=base,
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)

    archive_storage.persist_archive_message(
        shard,
        archive_storage.ArchiveMessage(
            chat_id=-1001,
            message_id=9,
            topic_id=77,
            sender_id=456,
            date=datetime(2026, 5, 1, 11, 59, tzinfo=timezone.utc),
            text="Question about ABC",
            raw_text="Question about ABC",
            message_kind="message",
            reply_to_msg_id=None,
            reply_to_top_id=77,
            is_forum_topic_link=True,
            has_media=True,
            media=(
                ArchiveMedia(
                    media_index=0,
                    media_kind="MessageMediaPhoto",
                    mime_type="image/jpeg",
                    file_size=9876,
                    file_name="abc-chart.jpg",
                ),
            ),
        ),
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    archive_storage.persist_archive_message(
        shard,
        archive_storage.ArchiveMessage(
            chat_id=-1001,
            message_id=10,
            topic_id=77,
            sender_id=123,
            date=base,
            text="duplicate archive text",
            raw_text="duplicate archive text",
            message_kind="message",
            reply_to_msg_id=9,
            reply_to_top_id=77,
            is_forum_topic_link=True,
            has_media=False,
        ),
        tracked_db_path=config.storage.db_path,
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    archive_storage.persist_archive_message(
        shard,
        archive_storage.ArchiveMessage(
            chat_id=-1001,
            message_id=11,
            topic_id=88,
            sender_id=456,
            date=datetime(2026, 5, 1, 12, 0, 30, tzinfo=timezone.utc),
            text="Other topic should not appear",
            raw_text="Other topic should not appear",
            message_kind="message",
            reply_to_msg_id=None,
            reply_to_top_id=88,
            is_forum_topic_link=True,
            has_media=False,
        ),
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    archive_storage.persist_archive_message(
        shard,
        archive_storage.ArchiveMessage(
            chat_id=-1001,
            message_id=12,
            topic_id=None,
            sender_id=456,
            date=datetime(2026, 5, 1, 12, 0, 45, tzinfo=timezone.utc),
            text="General should not appear",
            raw_text="General should not appear",
            message_kind="message",
            reply_to_msg_id=None,
            reply_to_top_id=None,
            is_forum_topic_link=False,
            has_media=False,
        ),
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    shard.close()
    manifest.close()

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=10,
        before_minutes=5,
        after_minutes=1,
        topic_id=77,
    )

    out = capsys.readouterr().out
    assert result == 0
    assert "Context filters: before 5m, after 1m, topic 77" in out
    assert "Target archived row: yes" in out
    assert "Question about ABC" in out
    assert "(media:MessageMediaPhoto/image/jpeg/9876B/abc-chart.jpg)" in out
    assert "tracked_ref" in out
    assert "reply:9,top:77" in out
    assert "tracked target text" in out
    assert "Reply snapshot: tracked reply snapshot" in out
    assert "duplicate archive text" not in out
    assert "Other topic should not appear" not in out
    assert "General should not appear" not in out


def test_archive_context_topic_filter_reports_target_topic_mismatch(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    tracked = tracked_storage.connect(config.storage.db_path)
    tracked_storage.ensure_schema(tracked)
    tracked_storage.persist_message(
        tracked,
        tracked_storage.StoredMessage(
            chat_id=-1001,
            message_id=10,
            sender_id=123,
            date=base,
            text="tracked target in another topic",
            reply_to_msg_id=None,
            replied_sender_id=None,
            replied_date=None,
            replied_text=None,
        ),
        [],
    )
    tracked.close()

    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=base,
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)
    try:
        archive_storage.persist_archive_message(
            shard,
            archive_storage.ArchiveMessage(
                chat_id=-1001,
                message_id=10,
                topic_id=88,
                sender_id=123,
                date=base,
                text="duplicate target text",
                raw_text="duplicate target text",
                message_kind="message",
                reply_to_msg_id=None,
                reply_to_top_id=88,
                is_forum_topic_link=True,
                has_media=False,
            ),
            tracked_db_path=config.storage.db_path,
        )
        archive_storage.record_shard_write(manifest, shard_meta)
        archive_storage.persist_archive_message(
            shard,
            archive_storage.ArchiveMessage(
                chat_id=-1001,
                message_id=11,
                topic_id=77,
                sender_id=456,
                date=base + timedelta(seconds=30),
                text="requested topic context",
                raw_text="requested topic context",
                message_kind="message",
                reply_to_msg_id=None,
                reply_to_top_id=77,
                is_forum_topic_link=True,
                has_media=False,
            ),
        )
        archive_storage.record_shard_write(manifest, shard_meta)
    finally:
        shard.close()
        manifest.close()

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=10,
        before_minutes=1,
        after_minutes=1,
        topic_id=77,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Target archived row: no" in out
    assert "Target topic mismatch: archived topic 88, requested topic 77" in out
    assert "requested topic context" in out
    assert "tracked target in another topic" not in out
    assert "duplicate target text" not in out


def test_archive_context_returns_nonzero_when_additive_schema_is_missing(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    tracked = tracked_storage.connect(config.storage.db_path)
    tracked_storage.ensure_schema(tracked)
    tracked_storage.persist_message(
        tracked,
        tracked_storage.StoredMessage(
            chat_id=-1001,
            message_id=10,
            sender_id=123,
            date=base,
            text="tracked target text",
            reply_to_msg_id=None,
            replied_sender_id=None,
            replied_date=None,
            replied_text=None,
        ),
        [],
    )
    tracked.close()
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    shard_meta = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=base,
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(shard_meta.path)
    archive_storage.persist_archive_message(
        shard,
        archive_storage.ArchiveMessage(
            chat_id=-1001,
            message_id=10,
            topic_id=None,
            sender_id=123,
            date=base,
            text="duplicate archive text",
            raw_text="duplicate archive text",
            message_kind="message",
            reply_to_msg_id=None,
            reply_to_top_id=None,
            is_forum_topic_link=False,
            has_media=False,
        ),
        tracked_db_path=config.storage.db_path,
    )
    archive_storage.record_shard_write(manifest, shard_meta)
    shard.execute("DROP TABLE archive_media")
    shard.close()
    manifest.close()

    result = _run_archive_context_command(
        config,
        chat_id=-1001,
        message_id=10,
        before_minutes=1,
        after_minutes=1,
        topic_id=None,
    )

    out = capsys.readouterr().out
    assert result == 1
    assert "Target archived row: yes" in out
    assert "tracked target text" in out
    assert "Errors:" in out
    assert "missing schema table(s): archive_media" in out


def test_archive_repair_disabled_does_not_create_archive_root(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert _run_archive_repair_command(config, apply=False) == 2
    assert not config.full_archive.root_dir.exists()


def test_archive_repair_missing_manifest_is_noop_and_readonly(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert _run_archive_repair_command(config, apply=True) == 0

    output = capsys.readouterr().out
    assert "Mode: Applied" in output
    assert "Manifest exists: False" in output
    assert "Repaired shards: 0" in output
    assert not config.full_archive.root_dir.exists()


def test_archive_repair_returns_nonzero_for_orphaned_shards_without_manifest(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    orphaned_shard = (
        config.full_archive.root_dir
        / "shards"
        / "group_-1001"
        / "2026-05.sqlite3"
    )
    orphaned_shard.parent.mkdir(parents=True)
    sqlite3.connect(orphaned_shard).close()

    assert _run_archive_repair_command(config, apply=True) == 1

    output = capsys.readouterr().out
    assert "Manifest exists: False" in output
    assert "Skipped shards: 1" in output
    assert "archive root has shard file(s) but no manifest (count=1)" in output
    assert str(orphaned_shard) not in output
    assert orphaned_shard.exists()


def test_archive_repair_returns_nonzero_for_unregistered_shards_with_manifest(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    registered = archive_storage.select_shard(
        manifest,
        config.full_archive.root_dir,
        chat_id=-1001,
        message_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
        max_messages_per_shard=500_000,
        max_shard_size_bytes=1024 * 1024,
    )
    shard = archive_storage.connect(registered.path)
    archive_storage.ensure_shard_schema(shard)
    shard.close()
    manifest.close()
    unregistered_shard = (
        config.full_archive.root_dir
        / "shards"
        / "group_-1001"
        / "2026-05-999.sqlite3"
    )
    sqlite3.connect(unregistered_shard).close()

    assert _run_archive_repair_command(config, apply=True) == 1

    output = capsys.readouterr().out
    assert "archive root has unregistered shard file(s) (count=1)" in output
    assert str(unregistered_shard) not in output
    assert unregistered_shard.exists()


def test_archive_repair_returns_nonzero_for_skipped_missing_shard(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest = config.full_archive.root_dir / "manifest.sqlite3"
    manifest.parent.mkdir(parents=True)
    conn = sqlite3.connect(manifest)
    try:
        conn.execute(
            """
            CREATE TABLE archive_shards (
                shard_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                topic_id INTEGER,
                path TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO archive_shards (
                shard_id, chat_id, topic_id, path, starts_at, ends_at,
                message_count, file_size_bytes, status, created_at, closed_at
            ) VALUES (?, ?, NULL, ?, ?, ?, 0, 0, 'active', ?, NULL)
            """,
            (
                "-1001:2026-05:001",
                -1001,
                str(config.full_archive.root_dir / "shards/group_-1001/2026-05.sqlite3"),
                "2026-05-01T00:00:00+00:00",
                "2026-06-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    assert _run_archive_repair_command(config, apply=False) == 1


def test_archive_repair_returns_nonzero_for_incomplete_tracked_ref_metadata(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    try:
        shard_meta = archive_storage.select_shard(
            manifest,
            config.full_archive.root_dir,
            chat_id=-1001,
            message_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
            max_messages_per_shard=500_000,
            max_shard_size_bytes=1024 * 1024,
        )
        tracked_db_path = config.storage.db_path
        tracked = tracked_storage.connect(tracked_db_path)
        try:
            tracked_storage.ensure_schema(tracked)
            tracked_storage.persist_message(
                tracked,
                tracked_storage.StoredMessage(
                    chat_id=-1001,
                    message_id=1,
                    sender_id=123,
                    date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    text="tracked",
                    reply_to_msg_id=None,
                    replied_sender_id=None,
                    replied_date=None,
                    replied_text=None,
                ),
                [],
            )
        finally:
            tracked.close()
        shard = archive_storage.connect(shard_meta.path)
        try:
            archive_storage.persist_archive_message(
                shard,
                archive_storage.ArchiveMessage(
                    chat_id=-1001,
                    message_id=1,
                    topic_id=None,
                    sender_id=123,
                    date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    text="duplicate",
                    raw_text="duplicate",
                    message_kind="message",
                    reply_to_msg_id=None,
                    reply_to_top_id=None,
                    is_forum_topic_link=False,
                    has_media=False,
                ),
                tracked_db_path=tracked_db_path,
                archive_root_dir=config.full_archive.root_dir,
            )
            shard.execute(
                """
                UPDATE archive_messages
                SET tracked_db_path = NULL
                WHERE chat_id = ? AND message_id = ?
                """,
                (-1001, 1),
            )
            shard.execute(
                "DELETE FROM archive_tracked_links WHERE chat_id = ? AND message_id = ?",
                (-1001, 1),
            )
            shard.commit()
        finally:
            shard.close()
        archive_storage.record_tracked_db_link(
            manifest,
            tracked_db_path,
            archive_root_dir=config.full_archive.root_dir,
        )
        archive_storage.record_shard_write(manifest, shard_meta)
    finally:
        manifest.close()

    assert _run_archive_repair_command(config, apply=True) == 1

    output = capsys.readouterr().out
    assert "Mode: Applied" in output
    assert "Skipped shards: 1" in output
    assert "incomplete tracked_ref metadata (rows=1); cannot repair" in output
    status = archive_storage.inspect_archive_status(config.full_archive.root_dir)
    assert status.degraded is True


def test_archive_repair_reports_and_removes_stale_tracked_ref_media_rows(
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    try:
        shard_meta = archive_storage.select_shard(
            manifest,
            config.full_archive.root_dir,
            chat_id=-1001,
            message_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
            max_messages_per_shard=500_000,
            max_shard_size_bytes=1024 * 1024,
        )
        tracked = tracked_storage.connect(config.storage.db_path)
        try:
            tracked_storage.ensure_schema(tracked)
            tracked_storage.persist_message(
                tracked,
                tracked_storage.StoredMessage(
                    chat_id=-1001,
                    message_id=1,
                    sender_id=123,
                    date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    text="tracked",
                    reply_to_msg_id=None,
                    replied_sender_id=None,
                    replied_date=None,
                    replied_text=None,
                ),
                [],
            )
        finally:
            tracked.close()
        shard = archive_storage.connect(shard_meta.path)
        try:
            archive_storage.persist_archive_message(
                shard,
                archive_storage.ArchiveMessage(
                    chat_id=-1001,
                    message_id=1,
                    topic_id=None,
                    sender_id=123,
                    date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                    text="duplicate",
                    raw_text="duplicate",
                    message_kind="message",
                    reply_to_msg_id=None,
                    reply_to_top_id=None,
                    is_forum_topic_link=False,
                    has_media=False,
                ),
                tracked_db_path=config.storage.db_path,
                archive_root_dir=config.full_archive.root_dir,
            )
            shard.execute(
                """
                UPDATE archive_messages
                SET text = ?, raw_text = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                ("stale text", "stale raw text", -1001, 1),
            )
            shard.execute(
                """
                INSERT INTO archive_media (
                    chat_id, message_id, media_index, media_kind, mime_type,
                    file_size, file_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    -1001,
                    1,
                    0,
                    "MessageMediaPhoto",
                    "image/jpeg",
                    123,
                    "stale.jpg",
                    "2026-05-01T12:00:00+00:00",
                    "2026-05-01T12:00:00+00:00",
                ),
            )
            shard.commit()
        finally:
            shard.close()
        archive_storage.record_tracked_db_link(
            manifest,
            config.storage.db_path,
            archive_root_dir=config.full_archive.root_dir,
        )
        archive_storage.record_shard_write(manifest, shard_meta)
    finally:
        manifest.close()

    assert _run_archive_repair_command(config, apply=False) == 0
    dry_run_output = capsys.readouterr().out
    assert "Stale tracked_ref text rows to clear: 1" in dry_run_output
    assert "Stale tracked_ref media rows to remove: 1" in dry_run_output
    assert "Dry-run only. Re-run with --apply to repair archive metadata." in dry_run_output

    assert _run_archive_repair_command(config, apply=True) == 0
    applied_output = capsys.readouterr().out
    assert "Cleared stale tracked_ref text rows: 1" in applied_output
    assert "Removed stale tracked_ref media rows: 1" in applied_output
    status = archive_storage.inspect_archive_status(config.full_archive.root_dir)
    assert status.degraded is False


def test_archive_repair_prunes_missing_shard_manifest_rows(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -1001
        root_dir = "data/full_archive"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    manifest = archive_storage.connect(config.full_archive.root_dir / "manifest.sqlite3")
    try:
        archive_storage.select_shard(
            manifest,
            config.full_archive.root_dir,
            chat_id=-1001,
            message_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
            max_messages_per_shard=500_000,
            max_shard_size_bytes=1024 * 1024,
        )
    finally:
        manifest.close()

    assert (
        _run_archive_repair_command(
            config,
            apply=True,
            prune_missing_shards=True,
        )
        == 0
    )
    status = archive_storage.inspect_archive_status(config.full_archive.root_dir)
    assert status.shard_count == 0
    assert status.missing_shard_count == 0


def test_list_topics_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "list-topics",
            "--config",
            "config.toml",
            "--chat",
            "-100123",
            "--limit",
            "50",
            "--query",
            "FLT",
        ]
    )
    assert args.command == "list-topics"
    assert args.chat == "-100123"
    assert args.limit == 50
    assert args.query == "FLT"


def test_archive_qa_init_parser() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "archive-qa-init",
            "--config",
            "config.toml",
            "--output",
            "reports/full_archive_qa/custom.md",
            "--force",
        ]
    )
    assert args.command == "archive-qa-init"
    assert str(args.output) == "reports/full_archive_qa/custom.md"
    assert args.force is True


def test_archive_qa_init_help_rejects_docs_output_guidance(capsys) -> None:
    parser = build_parser()
    try:
        parser.parse_args(["archive-qa-init", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("archive-qa-init --help should exit")

    output = capsys.readouterr().out
    assert "--output" in output
    assert "gitignored path" in output
    assert "not docs/" in output


def test_archive_qa_template_is_available_as_package_data() -> None:
    package_template = resources.files("telegram_watch").joinpath(
        "templates",
        "REAL_TELEGRAM_QA_TEMPLATE.md",
    )
    assert package_template.is_file()

    text = package_template.read_text(encoding="utf-8")

    assert "# 全量消息归档真实 Telegram QA 记录模板" in text
    assert "archive-qa-init" in text
    assert "`archive-status` 初始状态：disabled / empty / ok / degraded" in text
    assert "如果是 disabled：停止验证" in text
    assert "## Context / tracked DB 诊断" in text
    assert "cannot read tracked DB for target message" in text
    assert "could not read current tracked DB" in text
    assert "Target topic mismatch" in text
    assert "archive-qa-init 生成时 full_archive.enabled" in text
    assert "<other_topic_tracked_message_id>" in text
    assert "service message 标识" in text
    assert 'message_kind = "service"' in text
    assert "Topic / General / 未知归类" in text
    assert "Topic / Reply 列能解释它归属该 Topic 的原因" in text
    assert "重建后重新遇到 tracked 消息时是否仍为 `tracked_ref`" in text
    assert "没有 archive 侧 tracked 正文或媒体元数据" in text
    assert "archive-qa-init 生成时 capture_scope" in text
    assert "archive-qa-init 生成时 topic_ids 数量" in text
    assert "archive-qa-init 生成时 backfill_limit_messages" in text
    assert "archive-qa-init 生成时 source_chat_id 状态" in text
    assert "CR 结论：离线通过，待真实 QA" in text
    assert "CR 结论依据：不得只写“测试通过”" in text
    assert "live capture、backfill 或未验证项证据" in text


def test_archive_qa_template_is_declared_as_setuptools_package_data() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    package_data = pyproject["tool"]["setuptools"]["package-data"]

    assert "telegram_watch" in package_data
    assert "templates/*.md" in package_data["telegram_watch"]


def test_archive_qa_package_template_matches_docs_template() -> None:
    docs_template = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "full-message-archive"
        / "REAL_TELEGRAM_QA_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    package_template = resources.files("telegram_watch").joinpath(
        "templates",
        "REAL_TELEGRAM_QA_TEMPLATE.md",
    ).read_text(encoding="utf-8")

    assert package_template == docs_template
    assert cli_module._archive_qa_template_text() == docs_template


def test_archive_qa_template_loader_does_not_fallback_to_docs(monkeypatch) -> None:
    class MissingTemplate:
        def read_text(self, *, encoding: str) -> str:
            raise FileNotFoundError("missing package template")

    class MissingResources:
        def joinpath(self, *_parts: str) -> MissingTemplate:
            return MissingTemplate()

    monkeypatch.setattr(
        cli_module.resources,
        "files",
        lambda _package: MissingResources(),
    )

    try:
        cli_module._archive_qa_template_text()
    except FileNotFoundError as exc:
        assert "missing package template" in str(exc)
    else:
        raise AssertionError("archive QA template loader must not fallback to docs")


def test_source_revision_label_returns_unknown_without_git(monkeypatch) -> None:
    def fail_git(*_args, **_kwargs):
        raise FileNotFoundError("git missing")

    monkeypatch.setattr(cli_module.subprocess, "run", fail_git)

    assert cli_module._source_revision_label() == "unknown"


def test_source_revision_label_marks_dirty_without_file_list(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_git(args, **_kwargs):
        calls.append(list(args))
        if "rev-parse" in args:
            return SimpleNamespace(stdout="abc123\n")
        return SimpleNamespace(stdout=" M SECRET_CONFIG_NAME\n")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_git)

    assert cli_module._source_revision_label() == "abc123 (dirty)"
    assert len(calls) == 2


def test_runtime_version_labels_uses_python_and_telethon_metadata(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.metadata, "version", lambda _package: "1.43.0")

    python_version, telethon_version = cli_module._runtime_version_labels()

    assert python_version.count(".") == 2
    assert telethon_version == "1.43.0"


def test_runtime_version_labels_marks_missing_telethon_unknown(monkeypatch) -> None:
    def missing_version(_package: str) -> str:
        raise cli_module.metadata.PackageNotFoundError("Telethon")

    monkeypatch.setattr(cli_module.metadata, "version", missing_version)

    _, telethon_version = cli_module._runtime_version_labels()

    assert telethon_version == "unknown"


def test_network_command_stops_before_config_when_telethon_is_incompatible(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "telethon_runtime_problem",
        lambda: "Telethon 1.44.0 is required; found 1.42.0.",
    )

    result = cli_module.main(
        ["once", "--config", str(tmp_path / "missing.toml"), "--since", "10m"]
    )

    assert result == 2
    assert "Runtime error" in capsys.readouterr().out


def test_archive_qa_config_labels_do_not_include_chat_or_topic_ids(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [full_archive]
        enabled = true
        source_chat_id = -2002
        capture_scope = "topics"
        topic_ids = [10, 20]
        backfill_limit_messages = 0
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    labels = cli_module._archive_qa_config_labels(config)

    assert labels == {
        "capture_scope": "topics",
        "topic_ids_count": "2",
        "backfill_limit_messages": "0",
        "source_chat_id_status": "已配置但不匹配 target",
    }
    assert "-2002" not in " ".join(labels.values())
    assert "10" not in labels["topic_ids_count"]
    assert "20" not in labels["topic_ids_count"]


def test_archive_qa_init_reports_missing_package_template(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    class MissingTemplate:
        def read_text(self, *, encoding: str) -> str:
            raise FileNotFoundError("missing package template")

    class MissingResources:
        def joinpath(self, *_parts: str) -> MissingTemplate:
            return MissingTemplate()

    monkeypatch.setattr(
        cli_module.resources,
        "files",
        lambda _package: MissingResources(),
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [reporting]
        reports_dir = "reports"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert _run_archive_qa_init_command(config, output=None, force=False) == 2

    output = capsys.readouterr().out
    assert "Archive QA init error" in output
    assert "template not found" in output
    assert "missing package template" in output
    assert not (config.reporting.reports_dir / "full_archive_qa").exists()


def test_archive_qa_init_creates_gitignored_report_draft(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    secret_api_hash = "SECRET_API_HASH_SHOULD_NOT_APPEAR"
    secret_session_path = "data/SECRET_SESSION_SHOULD_NOT_APPEAR.session"
    config_path.write_text(
        f"""
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "{secret_api_hash}"
        session_file = "{secret_session_path}"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [reporting]
        reports_dir = "reports"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr(cli_module, "_source_revision_label", lambda: "abc123 (dirty)")
    monkeypatch.setattr(cli_module, "_runtime_version_labels", lambda: ("3.11.9", "1.43.0"))

    assert _run_archive_qa_init_command(config, output=None, force=False) == 0

    draft_dir = config.reporting.reports_dir / "full_archive_qa"
    drafts = list(draft_dir.glob("REAL_TELEGRAM_QA_*.md"))
    assert len(drafts) == 1
    content = drafts[0].read_text(encoding="utf-8")
    assert "# 全量消息归档真实 Telegram QA 记录模板" in content
    assert "- 验证日期：" in content
    assert "- archive-qa-init 生成时 full_archive.enabled：false" in content
    assert "- tgwatch commit：abc123 (dirty)" in content
    assert "- Python 版本：3.11.9" in content
    assert "- Telethon 版本：1.43.0" in content
    assert "- archive-qa-init 生成时 capture_scope：whole_group" in content
    assert "- archive-qa-init 生成时 topic_ids 数量：0" in content
    assert "- archive-qa-init 生成时 backfill_limit_messages：10000" in content
    assert "- archive-qa-init 生成时 source_chat_id 状态：未配置" in content
    assert "SECRET" not in content
    assert "api_hash" in content
    assert secret_api_hash not in content
    assert secret_session_path not in content
    assert not config.full_archive.root_dir.exists()
    output = capsys.readouterr().out
    assert "full_archive is disabled in this config" in output
    assert "before real Telegram QA" in output
    assert "QA draft created:" in output
    assert "gitignored" in output
    assert secret_api_hash not in output
    assert secret_session_path not in output


def test_archive_qa_init_records_enabled_full_archive_state(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    config_path = tmp_path / "config.toml"
    output_path = tmp_path / "reports" / "full_archive_qa" / "qa.md"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"

        [reporting]
        reports_dir = "reports"

        [full_archive]
        enabled = true
        root_dir = "data/full_archive"
        source_chat_id = -1001
        capture_scope = "topics"
        topic_ids = [10, 20]
        backfill_limit_messages = 0
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    monkeypatch.setattr(cli_module, "_source_revision_label", lambda: "def456")
    monkeypatch.setattr(cli_module, "_runtime_version_labels", lambda: ("3.12.1", "unknown"))

    assert _run_archive_qa_init_command(config, output=output_path, force=False) == 0

    content = output_path.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "- archive-qa-init 生成时 full_archive.enabled：true" in content
    assert "- tgwatch commit：def456" in content
    assert "- Python 版本：3.12.1" in content
    assert "- Telethon 版本：unknown" in content
    assert "- archive-qa-init 生成时 capture_scope：topics" in content
    assert "- archive-qa-init 生成时 topic_ids 数量：2" in content
    assert "- archive-qa-init 生成时 backfill_limit_messages：0" in content
    assert "- archive-qa-init 生成时 source_chat_id 状态：已配置且匹配 target" in content
    assert "-1001" not in content
    assert "topic_ids = [10, 20]" not in content
    assert "full_archive is disabled in this config" not in output
    assert not config.full_archive.root_dir.exists()


def test_archive_qa_init_refuses_to_overwrite_without_force(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    output_path = tmp_path / "reports" / "full_archive_qa" / "qa.md"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("existing", encoding="utf-8")
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    assert _run_archive_qa_init_command(config, output=output_path, force=False) == 2
    assert output_path.read_text(encoding="utf-8") == "existing"
    assert "output already exists" in capsys.readouterr().out


def test_archive_qa_init_refuses_source_docs_output(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    output_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "full-message-archive"
        / "REAL_TELEGRAM_QA_TEST_SHOULD_NOT_CREATE.md"
    )
    try:
        assert _run_archive_qa_init_command(config, output=output_path, force=True) == 2
        assert not output_path.exists()
        output = capsys.readouterr().out
        assert "output must not be under docs/" in output
        assert "gitignored path" in output
    finally:
        if output_path.exists():
            output_path.unlink()


def test_list_topics_empty_result_prints_fallback(
    monkeypatch, tmp_path, capsys
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fake_run_list_topics(_config, *, chat, limit, query):
        assert chat == -100123
        assert limit == 50
        assert query is None
        return []

    monkeypatch.setattr(cli_module, "run_list_topics", fake_run_list_topics)

    assert (
        asyncio.run(
            _run_list_topics_command(
                config,
                chat="-100123",
                limit=50,
                query=None,
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "No forum topics returned" in output
    assert "Fallback: manually set full_archive.topic_ids" in output
    assert 'capture_scope = "whole_group"' in output


def test_list_topics_error_prints_fallback(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fake_run_list_topics(_config, *, chat, limit, query):
        raise ValueError("Cannot list topics for -100123")

    monkeypatch.setattr(cli_module, "run_list_topics", fake_run_list_topics)

    try:
        asyncio.run(
            _run_list_topics_command(
                config,
                chat="-100123",
                limit=50,
                query=None,
            )
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("list-topics errors must exit nonzero")

    output = capsys.readouterr().out
    assert "List topics error" in output
    assert "Fallback: manually set full_archive.topic_ids" in output


def test_list_topics_success_prints_topic_rows(monkeypatch, tmp_path, capsys) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
        config_version = 1.0

        [telegram]
        api_id = 42
        api_hash = "abcdefghijk"

        [target]
        target_chat_id = -1001
        tracked_user_ids = [123]

        [control]
        control_chat_id = -1002

        [storage]
        db_path = "data/app.sqlite3"
        media_dir = "data/media"
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)

    async def fake_run_list_topics(_config, *, chat, limit, query):
        return [
            SimpleNamespace(
                topic_id=1,
                title="General",
                top_message=1,
                pinned=False,
                closed=False,
                hidden=False,
            ),
            SimpleNamespace(
                topic_id=10,
                title="FLT",
                top_message=10,
                pinned=True,
                closed=False,
                hidden=False,
            )
        ]

    monkeypatch.setattr(cli_module, "run_list_topics", fake_run_list_topics)

    assert (
        asyncio.run(
            _run_list_topics_command(
                config,
                chat="-100123",
                limit=50,
                query="F",
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Topic ID" in output
    assert "Archive Use" in output
    assert "General" in output
    assert "whole_group" in output
    assert "FLT" in output
    assert "topic_ids" in output
    assert "pinned" in output
    assert "do not put 1 in full_archive.topic_ids" in output
