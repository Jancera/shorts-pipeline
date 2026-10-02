from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class PublishAuthError(Exception):
    """Raised by target.get_credentials() when no usable token is on disk."""
    def __init__(self, message: str, *, reason: str = "missing") -> None:
        super().__init__(message)
        self.reason = reason  # "missing" | "expired"


class PublishConfigError(Exception):
    """Raised by target.authorize() when the platform isn't configured yet."""


@dataclass(frozen=True)
class PublishTarget:
    key: str                       # "youtube" | "tiktok" — manifest dict key
    label: str                     # "YouTube" | "TikTok" — UI display
    supports_scheduling: bool      # True: platform can publish-at-a-future-time.
                                    # False: upload always publishes immediately;
                                    # any computed slot is display-only.
    is_configured: Callable[[Any], bool]
    get_credentials: Callable[[Any], Any]
    authorize: Callable[[Any], Any]
    account_label: Callable[[Any], str]
    build_client: Callable[[Any], Any]
    build_body: Callable[..., dict]
    upload: Callable[..., dict]
    parse_upload_error: Callable[[Exception], dict]  # -> {"message": str, "abort_batch": bool}
