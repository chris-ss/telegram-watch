"""Command-line interface for telegram-watch."""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from importlib import metadata, resources
from pathlib import Path
from typing import Sequence
from rich.console import Console

from .config import Config, ConfigError, load_config
from .full_archive_storage import (
    ArchiveContextMessage,
    ArchiveMedia,
    ArchiveRepairReport,
    ArchiveStatusReport,
    fetch_context_result,
    find_tracked_message_date,
    format_archive_sender_label,
    inspect_archive_status,
    repair_archive_metadata,
    tracked_message_date_lookup_error,
)
from .migration import detect_migration_needed, migrate_config
from .doctor import run_doctor
from .gui import run_gui
from .runner import (
    run_archive_backfill,
    run_archive_senders_backfill,
    run_daemon,
    run_list_topics,
    run_once,
    run_reply_cleanup,
)
from .telethon_compat import telethon_runtime_problem
from .timeutils import parse_since_spec, utc_now


_TELEGRAM_RUNTIME_COMMANDS = {
    "once",
    "run",
    "cleanup-replies",
    "archive-backfill",
    "archive-senders-backfill",
    "list-topics",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgwatch",
        description="Telegram user watcher",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to config TOML file",
    )
    common.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "doctor",
        help="Validate config and environment",
        parents=[common],
    )

    once_parser = subparsers.add_parser(
        "once",
        help="Fetch a window and generate a report",
        parents=[common],
    )
    once_parser.add_argument(
        "--since",
        required=True,
        help="Window spec (e.g. 10m, 2h, or ISO timestamp)",
    )
    once_parser.add_argument(
        "--target",
        help="Limit to a single target (target name or target_chat_id)",
    )
    once_parser.add_argument(
        "--push",
        action="store_true",
        help="Also push the generated report/messages to the control chat",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run watcher daemon",
        parents=[common],
    )
    run_parser.add_argument(
        "--yes-retention",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    cleanup_parser = subparsers.add_parser(
        "cleanup-replies",
        help="One-time cleanup for historical false reply snapshots",
        parents=[common],
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to the database (default: dry-run)",
    )
    cleanup_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip DB backup before apply (use with caution)",
    )
    backfill_parser = subparsers.add_parser(
        "archive-backfill",
        help="Backfill optional full-message archive from Telegram history",
        parents=[common],
    )
    backfill_parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Non-negative maximum messages to scan; 0 is a no-op that does not "
            "connect to Telegram (default: full_archive.backfill_limit_messages)"
        ),
    )
    backfill_mode = backfill_parser.add_mutually_exclusive_group()
    backfill_mode.add_argument(
        "--apply",
        action="store_true",
        help="Write matched messages to archive DB (default is dry-run)",
    )
    backfill_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan only without writing archive DB (default)",
    )
    sender_backfill_parser = subparsers.add_parser(
        "archive-senders-backfill",
        help="Backfill local sender display snapshots for full-message archive",
        parents=[common],
    )
    sender_backfill_parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Non-negative maximum distinct senders to resolve; 0 is a no-op "
            "that does not connect to Telegram (default: all missing senders)"
        ),
    )
    sender_backfill_mode = sender_backfill_parser.add_mutually_exclusive_group()
    sender_backfill_mode.add_argument(
        "--apply",
        action="store_true",
        help="Resolve and write sender snapshots (default is dry-run)",
    )
    sender_backfill_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Count missing sender snapshots without Telegram requests (default)",
    )
    subparsers.add_parser(
        "archive-status",
        help="Inspect optional full-message archive health without writing files",
        parents=[common],
    )
    repair_parser = subparsers.add_parser(
        "archive-repair",
        help="Repair optional full-message archive metadata (dry-run by default)",
        parents=[common],
    )
    repair_mode = repair_parser.add_mutually_exclusive_group()
    repair_mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply archive metadata repairs",
    )
    repair_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report repairs without writing files (default)",
    )
    repair_parser.add_argument(
        "--prune-missing-shards",
        action="store_true",
        help=(
            "Prune manifest rows for shard files that are already missing; "
            "dry-run by default, writes only with --apply, never deletes files"
        ),
    )
    context_parser = subparsers.add_parser(
        "archive-context",
        help="Print archived context around a tracked message without writing files",
        parents=[common],
    )
    context_parser.add_argument(
        "--chat",
        required=True,
        type=int,
        help="Tracked message chat ID",
    )
    context_parser.add_argument(
        "--message-id",
        required=True,
        type=int,
        help="Tracked message ID",
    )
    context_parser.add_argument(
        "--before-minutes",
        type=int,
        default=10,
        help="Non-negative minutes of context before the tracked message",
    )
    context_parser.add_argument(
        "--after-minutes",
        type=int,
        default=5,
        help="Non-negative minutes of context after the tracked message",
    )
    context_parser.add_argument(
        "--topic-id",
        type=int,
        help=(
            "Optional forum topic ID filter; must be > 1. "
            "Use whole-group context for General topic 1."
        ),
    )
    topics_parser = subparsers.add_parser(
        "list-topics",
        help="List Telegram forum topics for a group",
        parents=[common],
    )
    topics_parser.add_argument(
        "--chat",
        required=True,
        help="Source chat ID or username",
    )
    topics_parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum topics to fetch",
    )
    topics_parser.add_argument(
        "--query",
        help="Optional topic search query",
    )
    qa_parser = subparsers.add_parser(
        "archive-qa-init",
        help="Create a gitignored real Telegram QA record draft",
        parents=[common],
    )
    qa_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Output markdown path; use reports/ or another gitignored path, not docs/ "
            "(default: reporting.reports_dir/full_archive_qa/REAL_TELEGRAM_QA_<date>.md)"
        ),
    )
    qa_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing QA draft",
    )

    gui_parser = subparsers.add_parser(
        "gui",
        help="Launch local GUI to edit config",
    )
    gui_parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    gui_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the GUI server",
    )
    gui_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind the GUI server",
    )
    gui_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to config TOML file (default: config.toml)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if args.command in _TELEGRAM_RUNTIME_COMMANDS:
        runtime_problem = telethon_runtime_problem()
        if runtime_problem is not None:
            Console().print(f"[bold red]Runtime error:[/bold red] {runtime_problem}")
            return 2
    if args.command == "doctor":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        run_doctor(config)
        return 0
    elif args.command == "once":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        since = parse_since_spec(args.since, now=utc_now())
        return asyncio.run(
            _run_once_command(
                config,
                since,
                since_label=args.since,
                push=args.push,
                target_selector=args.target,
            )
        )
    elif args.command == "run":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        if not _confirm_retention(
            config.reporting.retention_days,
            auto_confirm=bool(getattr(args, "yes_retention", False)),
        ):
            logging.getLogger(__name__).warning("Run cancelled by user.")
            return 1
        return asyncio.run(_run_daemon_command(config))
    elif args.command == "cleanup-replies":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return asyncio.run(
            _run_cleanup_replies_command(
                config,
                apply=bool(args.apply),
                backup=not bool(args.no_backup),
            )
        )
    elif args.command == "archive-backfill":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return asyncio.run(
            _run_archive_backfill_command(
                config,
                limit=args.limit,
                apply=bool(args.apply),
                dry_run=bool(args.dry_run),
            )
        )
    elif args.command == "archive-senders-backfill":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return asyncio.run(
            _run_archive_senders_backfill_command(
                config,
                limit=args.limit,
                apply=bool(args.apply),
                dry_run=bool(args.dry_run),
            )
        )
    elif args.command == "archive-status":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return _run_archive_status_command(config)
    elif args.command == "archive-repair":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return _run_archive_repair_command(
            config,
            apply=bool(args.apply),
            prune_missing_shards=bool(args.prune_missing_shards),
        )
    elif args.command == "archive-context":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return _run_archive_context_command(
            config,
            chat_id=args.chat,
            message_id=args.message_id,
            before_minutes=args.before_minutes,
            after_minutes=args.after_minutes,
            topic_id=args.topic_id,
        )
    elif args.command == "list-topics":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return asyncio.run(
            _run_list_topics_command(
                config,
                chat=args.chat,
                limit=args.limit,
                query=args.query,
            )
        )
    elif args.command == "archive-qa-init":
        config = _load_config_or_exit(parser, args.config, command=args.command)
        return _run_archive_qa_init_command(
            config,
            output=args.output,
            force=bool(args.force),
        )
    elif args.command == "gui":
        run_gui(args.config, host=args.host, port=args.port)
        return 0
    else:  # pragma: no cover - argparse ensures command
        parser.error(f"Unknown command {args.command}")


def _load_config_or_exit(
    parser: argparse.ArgumentParser, path: Path, *, command: str
) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        if command in {
            "run",
            "once",
            "archive-backfill",
            "archive-senders-backfill",
            "archive-status",
            "archive-repair",
            "archive-context",
            "list-topics",
            "archive-qa-init",
        }:
            Console().print(f"[bold red]Config error:[/bold red] {exc}")
            if _maybe_migrate_config(path):
                Console().print(
                    "[green]Migration completed. Review config.toml and rerun the command.[/green]"
                )
                raise SystemExit(0)
            raise SystemExit(2)
        parser.error(str(exc))


def _maybe_migrate_config(path: Path) -> bool:
    needs, reason = detect_migration_needed(path)
    if not needs:
        return False
    prompt = (
        f"Config migration needed ({reason}). Create a backup and rewrite config.toml now? [y/N]: "
    )
    try:
        answer = input(prompt)
    except EOFError:
        return False
    if answer.strip().lower() not in {"y", "yes"}:
        return False
    result = migrate_config(path)
    if not result.ok:
        Console().print(f"[bold red]Migration failed:[/bold red] {result.status}")
        return False
    if result.backup_path:
        Console().print(f"Backup saved to {result.backup_path.name}.")
    return True


async def _run_once_command(
    config,
    since,
    since_label=None,
    push: bool = False,
    target_selector: str | None = None,
):
    try:
        report_paths = await run_once(
            config,
            since,
            push=push,
            since_label=since_label,
            target_selector=target_selector,
        )
    except ValueError as exc:
        Console().print(f"[bold red]Run once error:[/bold red] {exc}")
        raise SystemExit(2)
    logger = logging.getLogger(__name__)
    for report_path in report_paths:
        logger.info("Report generated at %s", report_path)
    return 0


async def _run_daemon_command(config):
    await run_daemon(config)
    return 0


async def _run_cleanup_replies_command(
    config: Config,
    *,
    apply: bool,
    backup: bool,
) -> int:
    stats = await run_reply_cleanup(config, apply=apply, backup=backup)
    action_word = "will be cleaned" if not apply else "cleaned"
    Console().print(
        "\n".join(
            [
                f"Scanned: {stats.scanned}",
                f"Skipped (non-forum): {stats.skipped_non_forum}",
                f"Kept (explicit reply): {stats.kept_explicit_reply}",
                f"Missing from Telegram: {stats.missing_messages}",
                f"Candidates that {action_word}: {stats.to_clear}",
                f"Cleared messages: {stats.cleared_messages}",
                f"Cleared reply media rows: {stats.cleared_media}",
                (
                    f"Backup: {stats.backup_path}"
                    if stats.backup_path
                    else "Backup: not created"
                ),
            ]
        )
    )
    if not apply:
        Console().print("Dry-run only. Re-run with --apply to persist changes.")
    return 0


async def _run_archive_backfill_command(
    config: Config,
    *,
    limit: int | None,
    apply: bool,
    dry_run: bool = False,
) -> int:
    if apply and dry_run:
        Console().print(
            "[bold red]Archive backfill error:[/bold red] "
            "--apply and --dry-run cannot be used together"
        )
        return 2
    effective_limit = _archive_backfill_effective_limit(config, limit)
    if effective_limit < 0:
        Console().print(
            "[bold red]Archive backfill error:[/bold red] "
            "archive-backfill limit must be >= 0"
        )
        raise SystemExit(2)
    if apply and effective_limit != 0:
        preflight_result = _archive_backfill_apply_preflight(config)
        if preflight_result != 0:
            return preflight_result
    try:
        stats = await run_archive_backfill(config, limit=limit, apply=apply)
    except ValueError as exc:
        Console().print(f"[bold red]Archive backfill error:[/bold red] {exc}")
        raise SystemExit(2)
    Console().print(
        "\n".join(
            [
                f"Scanned: {stats.scanned}",
                f"Matched: {stats.matched}",
                f"Skipped (scope): {stats.skipped_scope}",
                f"Skipped (invalid): {stats.skipped_invalid}",
                f"Archived rows: {stats.archived}",
                f"Tracked links: {stats.linked}",
                f"Updated rows: {stats.updated}",
            ]
        )
    )
    if stats.dry_run:
        Console().print("Dry-run only. Re-run with --apply to persist archive rows.")
    return 0


async def _run_archive_senders_backfill_command(
    config: Config,
    *,
    limit: int | None,
    apply: bool,
    dry_run: bool = False,
) -> int:
    if apply and dry_run:
        Console().print(
            "[bold red]Archive sender backfill error:[/bold red] "
            "--apply and --dry-run cannot be used together"
        )
        return 2
    if limit is not None and limit < 0:
        Console().print(
            "[bold red]Archive sender backfill error:[/bold red] "
            "archive-senders-backfill limit must be >= 0"
        )
        raise SystemExit(2)
    if apply and limit != 0:
        preflight_result = _archive_senders_backfill_apply_preflight(config)
        if preflight_result != 0:
            return preflight_result
    try:
        stats = await run_archive_senders_backfill(
            config,
            limit=limit,
            apply=apply,
        )
    except ValueError as exc:
        Console().print(f"[bold red]Archive sender backfill error:[/bold red] {exc}")
        raise SystemExit(2)
    Console().print(
        "\n".join(
            [
                f"Missing sender snapshots: {stats.candidates}",
                f"Sender schema updates: {stats.schema_updates}",
                f"Reused archive snapshots: {stats.reused}",
                f"Resolved from session cache: {stats.cached}",
                f"Resolved from Telegram history: {stats.fetched}",
                f"Unresolved senders: {stats.unresolved}",
                f"Written senders: {stats.written_senders}",
                f"Shard writes: {stats.shard_writes}",
            ]
        )
    )
    if stats.dry_run:
        Console().print(
            "Dry-run only. Re-run with --apply to resolve and persist sender snapshots."
        )
    return 0


def _archive_backfill_effective_limit(config: Config, limit: int | None) -> int:
    return limit if limit is not None else config.full_archive.backfill_limit_messages


def _archive_backfill_apply_preflight(config: Config) -> int:
    archive = config.full_archive
    if not archive.enabled:
        return 0
    report = inspect_archive_status(
        archive.root_dir,
        tracked_db_path=config.storage.db_path,
    )
    if not report.degraded:
        return 0

    Console().print(
        "[bold red]Archive backfill error:[/bold red] archive health is degraded; "
        "run archive-status and archive-repair --dry-run before --apply."
    )
    if report.errors:
        Console().print("Archive health errors:")
        for error in report.errors:
            Console().print(f"- {error}")
    return 2


def _archive_senders_backfill_apply_preflight(config: Config) -> int:
    archive = config.full_archive
    if not archive.enabled:
        return 0
    report = inspect_archive_status(
        archive.root_dir,
        tracked_db_path=config.storage.db_path,
    )
    if not report.degraded or _only_archive_sender_schema_is_missing(report):
        return 0

    Console().print(
        "[bold red]Archive sender backfill error:[/bold red] "
        "archive health is degraded; run archive-status and "
        "archive-repair --dry-run before --apply."
    )
    if report.errors:
        Console().print("Archive health errors:")
        for error in report.errors:
            Console().print(f"- {error}")
    return 2


def _only_archive_sender_schema_is_missing(report: ArchiveStatusReport) -> bool:
    expected_errors = tuple(
        f"{shard.shard_id}: missing schema table(s): archive_senders"
        for shard in report.shards
        if shard.missing_schema_tables == ("archive_senders",)
    )
    return bool(expected_errors) and bool(
        report.missing_shard_count == 0
        and report.missing_index_count == 0
        and report.missing_schema_table_count == len(expected_errors)
        and report.errors == expected_errors
    )


def _run_archive_status_command(config: Config) -> int:
    archive = config.full_archive
    Console().print(f"Enabled: {archive.enabled}")
    Console().print(f"Root: {archive.root_dir}")
    if not archive.enabled:
        Console().print("Status: disabled")
        return 0

    report = inspect_archive_status(
        archive.root_dir,
        tracked_db_path=config.storage.db_path,
    )
    _print_archive_status_report(report)
    return 1 if report.degraded else 0


def _print_archive_status_report(report: ArchiveStatusReport) -> None:
    status = "degraded" if report.degraded else "ok"
    if not report.manifest_exists and not report.degraded:
        status = "empty"
    Console().print(f"Status: {status}")
    Console().print(f"Manifest: {report.manifest_path}")
    Console().print(f"Manifest exists: {report.manifest_exists}")
    Console().print(f"Shards: {report.shard_count}")
    Console().print(f"Missing shards: {report.missing_shard_count}")
    Console().print(f"Manifest message count: {report.manifest_message_count}")
    Console().print(f"Actual message count: {report.actual_message_count}")
    Console().print(f"Archive rows: {report.archive_row_count}")
    Console().print(f"Tracked refs: {report.tracked_ref_count}")
    Console().print(f"Tracked links: {report.link_count}")
    Console().print(f"Archive media metadata rows: {report.media_metadata_count}")
    Console().print(f"Tracked DB links: {report.tracked_db_link_count}")
    Console().print(
        f"Current tracked DB linked: {_format_optional_bool(report.current_tracked_db_linked)}"
    )
    Console().print(
        "Current tracked DB readable: "
        f"{_format_optional_bool(report.current_tracked_db_readable)}"
    )
    Console().print(f"Missing indexes: {report.missing_index_count}")
    Console().print(f"Missing schema tables: {report.missing_schema_table_count}")
    Console().print(f"Total DB bytes: {report.file_size_bytes}")
    if report.errors:
        Console().print("Errors:")
        for error in report.errors:
            Console().print(f"- {error}")


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _run_archive_repair_command(
    config: Config,
    *,
    apply: bool,
    prune_missing_shards: bool = False,
) -> int:
    archive = config.full_archive
    if not archive.enabled:
        Console().print("[bold red]Archive repair error:[/bold red] full_archive is disabled")
        return 2
    report = repair_archive_metadata(
        archive.root_dir,
        apply=apply,
        prune_missing_shards=prune_missing_shards,
    )
    _print_archive_repair_report(report)
    return 1 if report.errors or report.skipped_shards else 0


def _print_archive_repair_report(report: ArchiveRepairReport) -> None:
    action = "Dry-run" if report.dry_run else "Applied"
    Console().print(f"Mode: {action}")
    Console().print(f"Manifest: {report.manifest_path}")
    Console().print(f"Manifest exists: {report.manifest_exists}")
    Console().print(f"Checked shards: {report.checked_shards}")
    label = "Shards to repair" if report.dry_run else "Repaired shards"
    index_label = "Indexes to repair" if report.dry_run else "Repaired indexes"
    schema_label = (
        "Schema tables to repair" if report.dry_run else "Repaired schema tables"
    )
    manifest_label = (
        "Manifest metadata to repair"
        if report.dry_run
        else "Repaired manifest metadata"
    )
    link_label = "Tracked links to repair" if report.dry_run else "Repaired tracked links"
    stale_payload_label = (
        "Stale tracked_ref text rows to clear"
        if report.dry_run
        else "Cleared stale tracked_ref text rows"
    )
    stale_media_label = (
        "Stale tracked_ref media rows to remove"
        if report.dry_run
        else "Removed stale tracked_ref media rows"
    )
    Console().print(f"{label}: {report.repaired_shards}")
    Console().print(f"{index_label}: {report.repaired_indexes}")
    Console().print(f"{schema_label}: {report.repaired_schema_tables}")
    Console().print(f"{manifest_label}: {report.repaired_manifest_metadata}")
    Console().print(f"{link_label}: {report.repaired_link_rows}")
    Console().print(f"{stale_payload_label}: {report.repaired_stale_payload_rows}")
    Console().print(f"{stale_media_label}: {report.repaired_stale_media_rows}")
    prune_label = (
        "Missing shard records to prune"
        if report.dry_run
        else "Pruned missing shard records"
    )
    Console().print(f"{prune_label}: {report.pruned_missing_shards}")
    Console().print(f"Skipped shards: {report.skipped_shards}")
    if report.skipped_reasons:
        Console().print("Skipped:")
        for reason in report.skipped_reasons:
            Console().print(f"- {reason}")
    if report.dry_run and (
        report.repaired_indexes
        or report.repaired_schema_tables
        or report.repaired_manifest_metadata
        or report.repaired_link_rows
        or report.repaired_stale_payload_rows
        or report.repaired_stale_media_rows
        or report.pruned_missing_shards
    ):
        Console().print("Dry-run only. Re-run with --apply to repair archive metadata.")
    if report.errors:
        Console().print("Errors:")
        for error in report.errors:
            Console().print(f"- {error}")


def _run_archive_context_command(
    config: Config,
    *,
    chat_id: int,
    message_id: int,
    before_minutes: int,
    after_minutes: int,
    topic_id: int | None,
) -> int:
    if not config.full_archive.enabled:
        Console().print("[bold red]Archive context error:[/bold red] full_archive is disabled")
        return 2
    if before_minutes < 0 or after_minutes < 0:
        Console().print(
            "[bold red]Archive context error:[/bold red] before/after minutes must be >= 0"
        )
        return 2
    if topic_id is not None and topic_id <= 1:
        Console().print(
            "[bold red]Archive context error:[/bold red] "
            "topic_id must be a Telegram forum topic ID > 1"
        )
        return 2

    tracked_lookup_error = tracked_message_date_lookup_error(config.storage.db_path)
    if tracked_lookup_error:
        Console().print(
            "[bold red]Archive context error:[/bold red] "
            f"cannot read tracked DB for target message ({tracked_lookup_error})"
        )
        return 2

    center = find_tracked_message_date(
        config.storage.db_path,
        chat_id=chat_id,
        message_id=message_id,
    )
    if center is None:
        Console().print(
            "[bold red]Archive context error:[/bold red] tracked message not found"
        )
        return 2

    since = center - timedelta(minutes=before_minutes)
    until = center + timedelta(minutes=after_minutes)
    result = fetch_context_result(
        config.full_archive.root_dir,
        config.storage.db_path,
        chat_id=chat_id,
        since=since,
        until=until,
        topic_id=topic_id,
        target_message_id=message_id,
    )
    rows = list(result.messages)
    target_archived = _has_target_context_row(rows, chat_id=chat_id, message_id=message_id)
    target_topic_mismatch = _has_target_topic_mismatch(
        result,
        requested_topic_id=topic_id,
        target_archived_in_rows=target_archived,
    )
    _print_context_window(
        center=center,
        since=since,
        until=until,
        before_minutes=before_minutes,
        after_minutes=after_minutes,
        topic_id=topic_id,
    )
    if not rows:
        Console().print("No archived context rows found.")
        Console().print("Target archived row: no")
        if target_topic_mismatch:
            _print_context_target_topic_mismatch(
                requested_topic_id=topic_id,
                archived_topic_id=result.target_archived_topic_id,
            )
        if result.skipped_shards:
            _print_context_skips(result.skipped_shards)
        if result.errors:
            _print_context_errors(result.errors)
            return 1
        if result.skipped_shards or target_topic_mismatch:
            return 1
        return 0

    Console().print(f"Target archived row: {'yes' if target_archived else 'no'}")
    if target_topic_mismatch:
        _print_context_target_topic_mismatch(
            requested_topic_id=topic_id,
            archived_topic_id=result.target_archived_topic_id,
        )
    Console().print(
        f"{'Date':<25} {'Target':<6} {'Sender':<32} {'Mode':<12} {'Topic':<10} {'Reply':<24} {'Message':<10}"
    )
    Console().print("-" * 134)
    for row in rows:
        sender_alias = (
            config.resolve_user_alias(row.sender_id, chat_id=row.chat_id)
            if row.sender_id is not None
            else None
        )
        Console().print(
            _format_context_row(
                row,
                target_chat_id=chat_id,
                target_message_id=message_id,
                sender_alias=sender_alias,
            )
        )
    if result.skipped_shards:
        _print_context_skips(result.skipped_shards)
    if result.errors:
        _print_context_errors(result.errors)
        return 1
    if result.skipped_shards or target_topic_mismatch:
        return 1
    return 0


def _print_context_window(
    *,
    center: datetime,
    since: datetime,
    until: datetime,
    before_minutes: int,
    after_minutes: int,
    topic_id: int | None,
) -> None:
    topic = "whole group" if topic_id is None else str(topic_id)
    Console().print(f"Tracked message time: {center.isoformat()}")
    Console().print(f"Context window: {since.isoformat()} -> {until.isoformat()}")
    Console().print(
        f"Context filters: before {before_minutes}m, after {after_minutes}m, topic {topic}"
    )


def _has_target_context_row(
    rows: Sequence[ArchiveContextMessage],
    *,
    chat_id: int,
    message_id: int,
) -> bool:
    return any(row.chat_id == chat_id and row.message_id == message_id for row in rows)


def _has_target_topic_mismatch(
    result: ArchiveContextResult,
    *,
    requested_topic_id: int | None,
    target_archived_in_rows: bool,
) -> bool:
    return (
        requested_topic_id is not None
        and result.target_archived
        and not target_archived_in_rows
        and result.target_archived_topic_id != requested_topic_id
    )


def _print_context_target_topic_mismatch(
    *,
    requested_topic_id: int | None,
    archived_topic_id: int | None,
) -> None:
    requested = "whole group" if requested_topic_id is None else str(requested_topic_id)
    archived = "-" if archived_topic_id is None else str(archived_topic_id)
    Console().print(
        "Target topic mismatch: "
        f"archived topic {archived}, requested topic {requested}. "
        "Re-run without --topic-id or with the archived topic to inspect the target row."
    )


def _print_context_skips(skipped_shards: tuple[str, ...]) -> None:
    Console().print("Skipped shards:")
    for reason in skipped_shards:
        Console().print(f"- {reason}")


def _print_context_errors(errors: tuple[str, ...]) -> None:
    Console().print("Errors:")
    for error in errors:
        Console().print(f"- {error}")


def _format_context_row(
    row: ArchiveContextMessage,
    *,
    target_chat_id: int | None = None,
    target_message_id: int | None = None,
    sender_alias: str | None = None,
) -> str:
    target = (
        "*"
        if row.chat_id == target_chat_id and row.message_id == target_message_id
        else ""
    )
    sender = _short_text(
        format_archive_sender_label(row.sender, alias=sender_alias),
        limit=32,
    )
    topic = "-" if row.topic_id is None else str(row.topic_id)
    reply = _format_context_reply(row)
    text = _short_text(row.effective_text)
    media = _format_context_media(row.media)
    text_and_media = " ".join(part for part in (text, media) if part)
    reply_snapshot = _short_text(row.tracked_replied_text)
    metadata = (
        f"{row.date.isoformat():<25} "
        f"{target:<6} "
        f"{sender:<32} "
        f"{row.payload_mode:<12} "
        f"{topic:<10} "
        f"{reply:<24} "
        f"{row.message_id:<10}"
    )
    lines = [metadata, f"  Text: {text_and_media}"]
    if row.payload_mode == "tracked_ref" and reply_snapshot:
        lines.append(f"  Reply snapshot: {reply_snapshot}")
    return "\n".join(lines)


def _format_context_reply(row: ArchiveContextMessage) -> str:
    parts: list[str] = []
    if row.reply_to_msg_id is not None:
        parts.append(f"reply:{row.reply_to_msg_id}")
    if row.reply_to_top_id is not None:
        parts.append(f"top:{row.reply_to_top_id}")
    return ",".join(parts) if parts else "-"


def _format_context_media(media: tuple[ArchiveMedia, ...]) -> str:
    if not media:
        return ""
    parts: list[str] = []
    for item in media:
        details = [item.media_kind]
        if item.mime_type:
            details.append(item.mime_type)
        if item.file_size is not None:
            details.append(f"{item.file_size}B")
        if item.file_name:
            details.append(item.file_name)
        parts.append("/".join(details))
    return _short_text("(media:" + ",".join(parts) + ")", limit=120)


def _short_text(value: str | None, *, limit: int = 140) -> str:
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


async def _run_list_topics_command(
    config: Config,
    *,
    chat: str,
    limit: int,
    query: str | None,
) -> int:
    try:
        chat_arg: int | str = int(chat)
    except ValueError:
        chat_arg = chat
    try:
        topics = await run_list_topics(
            config,
            chat=chat_arg,
            limit=limit,
            query=query,
        )
    except ValueError as exc:
        Console().print(f"[bold red]List topics error:[/bold red] {exc}")
        _print_topic_fallback_hint()
        raise SystemExit(2)
    if not topics:
        Console().print(
            "No forum topics returned. If this group uses topics, check account "
            "access and chat ID."
        )
        _print_topic_fallback_hint()
        return 0

    max_title = max(5, *(len(topic.title) for topic in topics))
    header = f"{'Topic ID':<12} {'Top Message':<12} {'Archive Use':<14} {'Flags':<12} Title"
    Console().print(header)
    Console().print("-" * (len(header) + max(0, max_title - 5)))
    has_general = False
    for topic in topics:
        archive_use = "topic_ids" if topic.topic_id > 1 else "whole_group"
        if topic.topic_id <= 1:
            has_general = True
        flags = ",".join(
            flag
            for flag, active in (
                ("pinned", topic.pinned),
                ("closed", topic.closed),
                ("hidden", topic.hidden),
            )
            if active
        )
        Console().print(
            f"{topic.topic_id:<12} "
            f"{str(topic.top_message or ''):<12} "
            f"{archive_use:<14} "
            f"{flags or '-':<12} "
            f"{topic.title}"
        )
    if has_general:
        Console().print(
            "Note: topic ID 1 is Telegram General. Use capture_scope = "
            '"whole_group" for General context; do not put 1 in full_archive.topic_ids.'
        )
    return 0


def _print_topic_fallback_hint() -> None:
    Console().print(
        "Fallback: manually set full_archive.topic_ids in config.toml, or use "
        'capture_scope = "whole_group" to archive the whole group.'
    )


def _run_archive_qa_init_command(
    config: Config,
    *,
    output: Path | None,
    force: bool,
) -> int:
    try:
        content = _archive_qa_template_text()
    except FileNotFoundError as exc:
        Console().print(
            "[bold red]Archive QA init error:[/bold red] "
            f"template not found: {exc}"
        )
        return 2
    date_label = utc_now().strftime("%Y-%m-%d")
    output_path = (
        output
        if output is not None
        else config.reporting.reports_dir
        / "full_archive_qa"
        / f"REAL_TELEGRAM_QA_{date_label}.md"
    )
    if _is_under_source_docs(output_path):
        Console().print(
            "[bold red]Archive QA init error:[/bold red] "
            "output must not be under docs/; use reports/ or another gitignored path."
        )
        return 2
    if output_path.exists() and not force:
        Console().print(
            "[bold red]Archive QA init error:[/bold red] "
            f"output already exists: {output_path}"
        )
        Console().print("Re-run with --force to overwrite it.")
        return 2
    if not config.full_archive.enabled:
        Console().print(
            "[yellow]Archive QA init warning:[/yellow] "
            "full_archive is disabled in this config; enable it before real Telegram QA."
        )
    content = content.replace("- 验证日期：", f"- 验证日期：{date_label}", 1)
    content = content.replace(
        "- archive-qa-init 生成时 full_archive.enabled：",
        "- archive-qa-init 生成时 full_archive.enabled："
        f"{str(config.full_archive.enabled).lower()}",
        1,
    )
    content = content.replace(
        "- tgwatch commit：",
        f"- tgwatch commit：{_source_revision_label()}",
        1,
    )
    python_version, telethon_version = _runtime_version_labels()
    content = content.replace(
        "- Python 版本：",
        f"- Python 版本：{python_version}",
        1,
    )
    content = content.replace(
        "- Telethon 版本：",
        f"- Telethon 版本：{telethon_version}",
        1,
    )
    archive_config_labels = _archive_qa_config_labels(config)
    content = content.replace(
        "- archive-qa-init 生成时 capture_scope：",
        "- archive-qa-init 生成时 capture_scope："
        f"{archive_config_labels['capture_scope']}",
        1,
    )
    content = content.replace(
        "- archive-qa-init 生成时 topic_ids 数量：",
        "- archive-qa-init 生成时 topic_ids 数量："
        f"{archive_config_labels['topic_ids_count']}",
        1,
    )
    content = content.replace(
        "- archive-qa-init 生成时 backfill_limit_messages：",
        "- archive-qa-init 生成时 backfill_limit_messages："
        f"{archive_config_labels['backfill_limit_messages']}",
        1,
    )
    content = content.replace(
        "- archive-qa-init 生成时 source_chat_id 状态："
        "未配置 / 已配置且匹配 target / 已配置但不匹配 target",
        "- archive-qa-init 生成时 source_chat_id 状态："
        f"{archive_config_labels['source_chat_id_status']}",
        1,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    Console().print(f"QA draft created: {output_path}")
    Console().print(
        "Keep this file under reports/ or another gitignored location, and redact "
        "phone numbers, api_hash, session paths, private group names, and real user names."
    )
    return 0


def _is_under_source_docs(path: Path) -> bool:
    source_docs = Path(__file__).resolve().parent.parent / "docs"
    try:
        path.resolve().relative_to(source_docs.resolve())
    except ValueError:
        return False
    return True


def _archive_qa_template_text() -> str:
    package_template = resources.files("telegram_watch").joinpath(
        "templates",
        "REAL_TELEGRAM_QA_TEMPLATE.md",
    )
    return package_template.read_text(encoding="utf-8")


def _source_revision_label() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    try:
        rev = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    label = rev.stdout.strip() or "unknown"
    if label == "unknown":
        return label
    try:
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return label
    return f"{label} (dirty)" if status.stdout.strip() else label


def _runtime_version_labels() -> tuple[str, str]:
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    try:
        telethon_version = metadata.version("Telethon")
    except metadata.PackageNotFoundError:
        telethon_version = "unknown"
    return python_version, telethon_version


def _archive_qa_config_labels(config: Config) -> dict[str, str]:
    archive = config.full_archive
    target_chat_ids = {target.target_chat_id for target in config.targets}
    if archive.source_chat_id is None:
        source_status = "未配置"
    elif archive.source_chat_id in target_chat_ids:
        source_status = "已配置且匹配 target"
    else:
        source_status = "已配置但不匹配 target"
    return {
        "capture_scope": archive.capture_scope,
        "topic_ids_count": str(len(archive.topic_ids)),
        "backfill_limit_messages": str(archive.backfill_limit_messages),
        "source_chat_id_status": source_status,
    }


def _confirm_retention(retention_days: int, *, auto_confirm: bool = False) -> bool:
    if retention_days <= 180:
        return True
    if auto_confirm:
        return True
    prompt = (
        f"reporting.retention_days is set to {retention_days} days. "
        "This may consume significant disk space. Continue? [y/N]: "
    )
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
