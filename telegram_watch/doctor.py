"""Doctor command implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Iterable
from rich.console import Console
from rich.table import Table

from .config import Config
from .full_archive_storage import connect as connect_archive
from .full_archive_storage import ensure_manifest_schema
from .storage import db_session
from .telethon_compat import (
    SUPPORTED_TELETHON_LAYER,
    SUPPORTED_TELETHON_VERSION,
    telethon_runtime_problem,
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warn: bool = False


def run_doctor(config: Config) -> None:
    """Validate config and local environment."""
    console = Console()
    checks: list[CheckResult] = []
    checks.append(CheckResult("config", True, "config parsed successfully"))
    checks.append(_check_telethon_runtime())

    checks.extend(
        [
            _check_dir("session dir", config.telegram.session_file.parent),
            _check_dir("media dir", config.storage.media_dir),
            _check_dir("reports dir", config.reporting.reports_dir),
        ]
    )
    if config.sender is not None:
        checks.append(_check_dir("sender session dir", config.sender.session_file.parent))

    checks.extend(_check_cloud_sync("session file", config.telegram.session_file))
    checks.extend(_check_cloud_sync("database", config.storage.db_path))

    checks.append(_check_db(config))
    checks.extend(_check_full_archive(config))

    table = Table(title="telegram-watch doctor", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail")
    success = True
    for result in checks:
        if result.warn:
            status = "[yellow]WARN[/yellow]"
        elif result.ok:
            status = "[green]OK[/green]"
        else:
            status = "[red]FAIL[/red]"
        table.add_row(result.name, status, result.detail)
        if not result.ok and not result.warn:
            success = False

    console.print(table)
    if not success:
        raise SystemExit("doctor failed")


def _check_dir(label: str, path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
        _probe_dir_writable(path)
    except OSError as exc:
        return CheckResult(label, False, f"cannot create or write {path}: {exc}")
    return CheckResult(label, True, f"{path}")


def _check_telethon_runtime() -> CheckResult:
    problem = telethon_runtime_problem()
    if problem is not None:
        return CheckResult("Telethon runtime", False, problem)
    return CheckResult(
        "Telethon runtime",
        True,
        f"{SUPPORTED_TELETHON_VERSION} (layer {SUPPORTED_TELETHON_LAYER})",
    )


def _probe_dir_writable(path: Path) -> None:
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path,
            prefix=".tgwatch-write-test-",
            encoding="utf-8",
            delete=False,
        ) as fh:
            probe_path = Path(fh.name)
            fh.write("ok")
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink()
            except FileNotFoundError:
                pass


_CLOUD_SYNC_PATTERNS: dict[str, tuple[str, ...]] = {
    "Dropbox": ("/Dropbox/", "/Library/CloudStorage/Dropbox/"),
    "iCloud": (
        "/Library/Mobile Documents/",
        "/Library/CloudStorage/iCloud Drive/",
        "/iCloud/",
    ),
    "OneDrive": ("/OneDrive/", "/Library/CloudStorage/OneDrive"),
    "Google Drive": (
        "/Google Drive/",
        "/GoogleDrive/",
        "/Library/CloudStorage/GoogleDrive",
    ),
}


def _check_cloud_sync(label: str, path: Path) -> list[CheckResult]:
    """Warn if *path* sits inside a known cloud sync directory."""
    results: list[CheckResult] = []
    abs_path = str(path.resolve())
    for service, markers in _CLOUD_SYNC_PATTERNS.items():
        if any(marker in abs_path for marker in markers):
            results.append(
                CheckResult(
                    name=f"cloud sync ({label})",
                    ok=True,
                    detail=(
                        f"{label} is inside a cloud sync directory ({service}). "
                        "Cloud sync can cause SQLite lock conflicts. Consider moving "
                        "data files outside the sync folder, or ensure WAL mode is "
                        "enabled (default since v1.6.1)."
                    ),
                    warn=True,
                )
            )
    return results


def _check_db(config: Config) -> CheckResult:
    try:
        with db_session(config.storage.db_path) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - surfaces in console only
        return CheckResult("database", False, f"{exc}")
    return CheckResult("database", True, f"{config.storage.db_path}")


def _check_full_archive(config: Config) -> list[CheckResult]:
    archive = config.full_archive
    if not archive.enabled:
        return [CheckResult("full archive", True, "disabled")]

    dir_results = [
        _check_dir("full archive dir", archive.root_dir),
        _check_dir("full archive shards dir", archive.root_dir / "shards"),
    ]
    results = list(dir_results)
    target_chat_ids = {target.target_chat_id for target in config.targets}
    if archive.source_chat_id not in target_chat_ids:
        results.append(
            CheckResult(
                "full archive source",
                True,
                (
                    "source_chat_id is not one of the configured target chats; "
                    "context recovery for tracked messages may be unavailable"
                ),
                warn=True,
            )
        )
    results.extend(
        _check_cloud_sync("full archive", archive.root_dir / "manifest.sqlite3")
    )
    if any(not result.ok and not result.warn for result in dir_results):
        return results

    manifest_path = archive.root_dir / "manifest.sqlite3"
    try:
        conn = connect_archive(manifest_path)
        try:
            ensure_manifest_schema(conn)
            conn.execute("SELECT 1 FROM archive_shards LIMIT 1")
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - surfaces in console only
        results.append(CheckResult("full archive manifest", False, f"{exc}"))
    else:
        results.append(CheckResult("full archive manifest", True, f"{manifest_path}"))
    return results
