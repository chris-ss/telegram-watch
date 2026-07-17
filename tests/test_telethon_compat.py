from datetime import datetime, timezone
import struct

from telethon.extensions.binaryreader import BinaryReader
from telethon.tl import alltlobjects, types

from telegram_watch import telethon_compat


def test_server_message_constructor_uses_tested_telethon_reader() -> None:
    message = types.Message(
        id=123,
        peer_id=types.PeerUser(user_id=456),
        date=datetime(2026, 7, 9, tzinfo=timezone.utc),
        message="synthetic message",
        offline=True,
        schedule_repeat_period=60,
        summary_from_language="en",
    )
    current_payload = bytes(message)
    server_payload = (
        struct.pack("<I", telethon_compat.SERVER_MESSAGE_CONSTRUCTOR_ID)
        + current_payload[4:]
    )

    parsed = BinaryReader(server_payload).tgread_object()

    assert isinstance(parsed, types.Message)
    assert parsed.id == 123
    assert parsed.peer_id == types.PeerUser(user_id=456)
    assert parsed.message == "synthetic message"
    assert parsed.offline is True
    assert parsed.schedule_repeat_period == 60
    assert parsed.summary_from_language == "en"


def test_unsupported_telethon_version_does_not_install_alias(monkeypatch) -> None:
    monkeypatch.delitem(
        alltlobjects.tlobjects,
        telethon_compat.SERVER_MESSAGE_CONSTRUCTOR_ID,
        raising=False,
    )
    monkeypatch.setattr(telethon_compat.metadata, "version", lambda _name: "1.42.0")

    installed = telethon_compat.install_message_constructor_compat()

    assert installed is False
    assert telethon_compat.SERVER_MESSAGE_CONSTRUCTOR_ID not in alltlobjects.tlobjects
    assert "1.44.0 is required" in telethon_compat.telethon_runtime_problem()


def test_existing_constructor_registration_is_not_overwritten(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setitem(
        alltlobjects.tlobjects,
        telethon_compat.SERVER_MESSAGE_CONSTRUCTOR_ID,
        sentinel,
    )

    installed = telethon_compat.install_message_constructor_compat()

    assert installed is False
    assert (
        alltlobjects.tlobjects[telethon_compat.SERVER_MESSAGE_CONSTRUCTOR_ID]
        is sentinel
    )
