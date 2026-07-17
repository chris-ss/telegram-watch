from pathlib import Path
from textwrap import dedent

from telegram_watch.config import load_config
from telegram_watch import doctor as doctor_mod
from telegram_watch.doctor import (
    _check_full_archive,
    _check_telethon_runtime,
    run_doctor,
)


def write_config(tmp_path: Path, body: str) -> Path:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(dedent(body).lstrip(), encoding="utf-8")
    return cfg_path


def base_config(extra: str = "") -> str:
    return f"""
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

    {extra}
    """


def test_full_archive_doctor_disabled_does_not_create_archive_dir(tmp_path):
    config = load_config(write_config(tmp_path, base_config()))

    results = _check_full_archive(config)

    assert len(results) == 1
    assert results[0].name == "full archive"
    assert results[0].ok is True
    assert results[0].detail == "disabled"
    assert not config.full_archive.root_dir.exists()


def test_telethon_runtime_check_reports_incompatible_version(monkeypatch):
    monkeypatch.setattr(
        doctor_mod,
        "telethon_runtime_problem",
        lambda: "Telethon 1.44.0 is required; found 1.42.0.",
    )

    result = _check_telethon_runtime()

    assert result.ok is False
    assert result.name == "Telethon runtime"
    assert "found 1.42.0" in result.detail


def test_full_archive_doctor_enabled_creates_manifest(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            base_config(
                """
                [full_archive]
                enabled = true
                root_dir = "data/full_archive"
                source_chat_id = -1001
                """
            ),
        )
    )

    results = _check_full_archive(config)

    assert all(result.ok for result in results if not result.warn)
    assert (config.full_archive.root_dir / "manifest.sqlite3").exists()
    assert (config.full_archive.root_dir / "shards").is_dir()
    assert not list((config.full_archive.root_dir / "shards").glob("group_*"))


def test_full_archive_doctor_reports_shards_dir_check(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            base_config(
                """
                [full_archive]
                enabled = true
                root_dir = "data/full_archive"
                source_chat_id = -1001
                """
            ),
        )
    )

    results = _check_full_archive(config)

    names = [result.name for result in results]
    assert "full archive shards dir" in names


def test_full_archive_doctor_fails_when_archive_dir_write_probe_fails(
    monkeypatch,
    tmp_path,
):
    config = load_config(
        write_config(
            tmp_path,
            base_config(
                """
                [full_archive]
                enabled = true
                root_dir = "data/full_archive"
                source_chat_id = -1001
                """
            ),
        )
    )

    def fail_archive_root_probe(path: Path) -> None:
        if path == config.full_archive.root_dir:
            raise OSError("read-only archive root")

    monkeypatch.setattr(doctor_mod, "_probe_dir_writable", fail_archive_root_probe)

    results = _check_full_archive(config)

    root_check = next(result for result in results if result.name == "full archive dir")
    assert root_check.ok is False
    assert "cannot create or write" in root_check.detail
    assert "read-only archive root" in root_check.detail
    assert not (config.full_archive.root_dir / "manifest.sqlite3").exists()


def test_full_archive_doctor_warns_when_source_is_not_a_target(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            base_config(
                """
                [full_archive]
                enabled = true
                root_dir = "data/full_archive"
                source_chat_id = -2002
                """
            ),
        )
    )

    results = _check_full_archive(config)

    warning = next(
        result for result in results if result.name == "full archive source"
    )
    assert warning.warn is True
    assert warning.ok is True
    assert "not one of the configured target chats" in warning.detail


def test_run_doctor_prints_full_archive_shards_dir_when_enabled(
    tmp_path,
    capsys,
):
    config = load_config(
        write_config(
            tmp_path,
            base_config(
                """
                [full_archive]
                enabled = true
                root_dir = "data/full_archive"
                source_chat_id = -1001
                """
            ),
        )
    )

    run_doctor(config)

    output = capsys.readouterr().out
    assert "full archive shards dir" in output
    assert (config.full_archive.root_dir / "shards").is_dir()
    assert not list((config.full_archive.root_dir / "shards").glob("group_*"))


def test_full_archive_doctor_warns_for_modern_cloudstorage_paths(tmp_path):
    cloud_root = tmp_path / "Library" / "CloudStorage" / "iCloud Drive"
    config = load_config(
        write_config(
            tmp_path,
            base_config(
                f"""
                [full_archive]
                enabled = true
                root_dir = "{cloud_root / 'telegram-watch-archive'}"
                source_chat_id = -1001
                """
            ),
        )
    )

    results = _check_full_archive(config)

    warnings = [result for result in results if result.warn]
    assert any(result.name == "cloud sync (full archive)" for result in warnings)
    assert any("iCloud" in result.detail for result in warnings)
