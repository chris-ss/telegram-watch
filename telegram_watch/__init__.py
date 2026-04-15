"""telegram_watch package exports."""

from __future__ import annotations

__version__ = "1.7.0"

__all__ = [
    "__version__",
    "load_config",
    "Config",
]

from .config import Config, load_config  # noqa: E402  (import after __all__)
