from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from telegram_watch.config import (
    Config,
    ConfigError,
    ControlGroupConfig,
    DisplayConfig,
    NotificationConfig,
    RealtimeConfig,
    ReportingConfig,
    StorageConfig,
    TargetGroupConfig,
    TelegramConfig,
    VALID_DISPLAY_TEMPLATES,
)
from telegram_watch.runner import _format_control_message
from telegram_watch.storage import DbMedia, DbMessage


def build_config(tmp_path: Path) -> Config:
    telegram = TelegramConfig(api_id=1, api_hash="abcdefghijk", session_file=tmp_path / "session")
    target = TargetGroupConfig(
        name="default",
        target_chat_id=-123,
        tracked_user_ids=(111, 222),
        tracked_user_aliases=MappingProxyType({111: "Alice", 222: "Bob"}),
        summary_interval_minutes=120,
        control_group="default",
    )
    control = ControlGroupConfig(
        key="default",
        control_chat_id=-456,
        is_forum=False,
        topic_routing_enabled=False,
        topic_target_map=MappingProxyType({}),
    )
    storage = StorageConfig(db_path=tmp_path / "db.sqlite3", media_dir=tmp_path / "media")
    reporting = ReportingConfig(
        reports_dir=tmp_path / "reports",
        summary_interval_minutes=120,
        timezone=timezone.utc,
        retention_days=30,
    )
    display = DisplayConfig(
        show_ids=True,
        time_format="%Y.%m.%d %H:%M:%S (%Z)",
        language="auto",
    )
    notifications = NotificationConfig(bark_key=None, heartbeat_interval_hours=2, check_updates=True)
    return Config(
        config_version=1.0,
        telegram=telegram,
        sender=None,
        targets=(target,),
        control_groups=MappingProxyType({"default": control}),
        target_by_chat_id=MappingProxyType({target.target_chat_id: target}),
        target_by_name=MappingProxyType({target.name: target}),
        control_by_chat_id=MappingProxyType({control.control_chat_id: control}),
        targets_by_control=MappingProxyType({"default": (target,)}),
        storage=storage,
        reporting=reporting,
        display=display,
        notifications=notifications,
        realtime=RealtimeConfig(
            push_mode="interval",
            report_interval_minutes=120,
            rate_limit_per_minute=20,
            rate_limit_per_hour=200,
            rate_limit_per_day=1000,
            min_interval_sec=3.0,
            media_extra_delay_sec=2.0,
            warmup_minutes=5.0,
            warmup_rate=5,
        ),
    )


def _make_message(
    *,
    text: str | None = "Hello world",
    replied_sender_id: int | None = None,
    replied_text: str | None = None,
    replied_date: datetime | None = None,
    media: list[DbMedia] | None = None,
) -> DbMessage:
    return DbMessage(
        chat_id=-123,
        message_id=999,
        sender_id=111,
        date=datetime(2026, 1, 24, 15, 30, 45, tzinfo=timezone.utc),
        text=text,
        reply_to_msg_id=(888 if replied_sender_id else None),
        replied_sender_id=replied_sender_id,
        replied_date=replied_date,
        replied_text=replied_text,
        media=media or [],
    )


def _set_template(config, template: str):
    return replace(config, display=replace(config.display, template=template))


def test_normal_template_basic(tmp_path: Path):
    cfg = build_config(tmp_path)
    assert cfg.display.template == "normal", "default must stay normal"
    msg = _make_message()
    out = _format_control_message(msg, cfg, cfg.targets[0])
    lines = out.split("\n")
    assert lines[0] == "<b>Alice (111)</b>"
    assert lines[1].startswith("Time: ")
    assert "MSG 999" in lines[1]
    assert lines[2] == "<b>Content:</b> Hello world"
    assert len(lines) == 3


def test_minimal_template_basic(tmp_path: Path):
    cfg = _set_template(build_config(tmp_path), "minimal")
    msg = _make_message()
    out = _format_control_message(msg, cfg, cfg.targets[0])
    lines = out.split("\n")
    assert lines[0] == "<b>Alice (111)</b>: Hello world"
    assert lines[1].startswith("Time: ")
    assert "MSG 999" in lines[1]
    assert len(lines) == 2
    assert "<b>Content:</b>" not in out


def test_minimal_template_empty_body(tmp_path: Path):
    cfg = _set_template(build_config(tmp_path), "minimal")
    msg = _make_message(text=None)
    out = _format_control_message(msg, cfg, cfg.targets[0])
    assert out.startswith("<b>Alice (111)</b>: <i>no text</i>")


def test_minimal_template_respects_show_ids_off(tmp_path: Path):
    base = build_config(tmp_path)
    cfg = replace(
        base,
        display=replace(base.display, template="minimal", show_ids=False),
    )
    msg = _make_message()
    out = _format_control_message(msg, cfg, cfg.targets[0])
    lines = out.split("\n")
    assert lines[0] == "<b>Alice</b>: Hello world", "ID must be hidden when show_ids=False"


def test_minimal_template_respects_show_ids_on(tmp_path: Path):
    cfg = _set_template(build_config(tmp_path), "minimal")
    msg = _make_message()
    out = _format_control_message(msg, cfg, cfg.targets[0])
    assert out.startswith("<b>Alice (111)</b>: Hello world")


def test_minimal_template_with_attachments(tmp_path: Path):
    cfg = _set_template(build_config(tmp_path), "minimal")
    media = [
        DbMedia(media_index=0, file_path="a.jpg", mime_type="image/jpeg", file_size=100, is_reply=False),
        DbMedia(media_index=1, file_path="b.png", mime_type="image/png", file_size=200, is_reply=False),
    ]
    msg = _make_message(media=media)
    out = _format_control_message(msg, cfg, cfg.targets[0])
    assert "Attachments: 2 file(s) to follow." in out
    assert out.split("\n")[0] == "<b>Alice (111)</b>: Hello world"


def test_minimal_template_with_reply(tmp_path: Path):
    cfg = _set_template(build_config(tmp_path), "minimal")
    msg = _make_message(
        replied_sender_id=222,
        replied_text="original",
        replied_date=datetime(2026, 1, 24, 15, 29, 0, tzinfo=timezone.utc),
    )
    out = _format_control_message(msg, cfg, cfg.targets[0])
    assert "<blockquote>\u21a9 Reply to Bob (222)" in out
    assert "<blockquote>original</blockquote>" in out
    assert out.split("\n")[0] == "<b>Alice (111)</b>: Hello world"


def test_minimal_template_all_combined(tmp_path: Path):
    cfg = _set_template(build_config(tmp_path), "minimal")
    media = [
        DbMedia(media_index=0, file_path="a.jpg", mime_type="image/jpeg", file_size=100, is_reply=False),
        DbMedia(media_index=1, file_path="b.png", mime_type="image/png", file_size=200, is_reply=True),
    ]
    msg = _make_message(
        replied_sender_id=222,
        replied_text="original",
        replied_date=datetime(2026, 1, 24, 15, 29, 0, tzinfo=timezone.utc),
        media=media,
    )
    out = _format_control_message(msg, cfg, cfg.targets[0])
    lines = out.split("\n")
    assert lines[0] == "<b>Alice (111)</b>: Hello world"
    assert lines[1].startswith("Time: ")
    assert any("Attachments: 1 file(s)" in line for line in lines)
    assert any("Reply attachments: 1 file(s)" in line for line in lines)
    assert any("<blockquote>\u21a9 Reply to" in line for line in lines)


def test_display_config_template_default():
    cfg = DisplayConfig(show_ids=True, time_format="%H:%M:%S", language="auto")
    assert cfg.template == "normal"


def test_display_config_template_invalid_rejected():
    from telegram_watch.config import _parse_display

    with pytest.raises(ConfigError) as exc:
        _parse_display({"show_ids": True, "time_format": "%H:%M:%S", "language": "auto", "template": "fancy"})
    assert "display.template" in str(exc.value)
    assert "normal" in str(exc.value) and "minimal" in str(exc.value)


def test_display_config_template_legacy_missing_key_defaults_to_normal():
    from telegram_watch.config import _parse_display

    cfg = _parse_display({"show_ids": True, "time_format": "%H:%M:%S", "language": "auto"})
    assert cfg.template == "normal"


def test_display_config_valid_templates_constant_has_both():
    assert "normal" in VALID_DISPLAY_TEMPLATES
    assert "minimal" in VALID_DISPLAY_TEMPLATES
    assert len(VALID_DISPLAY_TEMPLATES) == 2
