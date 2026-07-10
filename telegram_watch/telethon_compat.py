"""Narrow compatibility support for the tested Telethon runtime."""

from __future__ import annotations

from importlib import metadata

from telethon.tl import alltlobjects, types


SUPPORTED_TELETHON_VERSION = "1.44.0"
SUPPORTED_TELETHON_LAYER = 227
TELETHON_MESSAGE_CONSTRUCTOR_ID = 0x7600B9D3
SERVER_MESSAGE_CONSTRUCTOR_ID = 0x3AE56482


def _base_runtime_problem() -> str | None:
    try:
        installed_version = metadata.version("Telethon")
    except metadata.PackageNotFoundError:
        return "Telethon is not installed. Re-run the launcher or `pip install -e .`."

    if installed_version != SUPPORTED_TELETHON_VERSION:
        return (
            f"Telethon {SUPPORTED_TELETHON_VERSION} is required; "
            f"found {installed_version}. Re-run the launcher or `pip install -e .`."
        )

    layer = getattr(alltlobjects, "LAYER", None)
    constructor_id = getattr(types.Message, "CONSTRUCTOR_ID", None)
    if layer != SUPPORTED_TELETHON_LAYER or constructor_id != TELETHON_MESSAGE_CONSTRUCTOR_ID:
        return (
            "The installed Telethon schema does not match the tested runtime "
            f"(expected layer {SUPPORTED_TELETHON_LAYER}, "
            f"Message 0x{TELETHON_MESSAGE_CONSTRUCTOR_ID:08x})."
        )
    return None


def install_message_constructor_compat() -> bool:
    """Register Telegram's compatible Message constructor when it is absent."""
    if _base_runtime_problem() is not None:
        return False
    if SERVER_MESSAGE_CONSTRUCTOR_ID in alltlobjects.tlobjects:
        return False

    # The server schema is a strict subset of Telethon 1.44's Message reader.
    # Telethon's two newer fields are guarded by flags the older schema never sets.
    alltlobjects.tlobjects[SERVER_MESSAGE_CONSTRUCTOR_ID] = types.Message
    return True


def telethon_runtime_problem() -> str | None:
    """Return a safe user-facing runtime problem, or None when ready."""
    problem = _base_runtime_problem()
    if problem is not None:
        return problem
    if SERVER_MESSAGE_CONSTRUCTOR_ID not in alltlobjects.tlobjects:
        return (
            "Telegram Message constructor compatibility was not installed. "
            "Restart tgwatch before connecting."
        )
    return None
