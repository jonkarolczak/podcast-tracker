"""Logging filters. Must be installed on the root logger before anything else logs."""
from __future__ import annotations

import logging
import os

SECRET_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "EXA_API_KEY",
    "PODCASTINDEX_API_KEY",
    "PODCASTINDEX_API_SECRET",
    "RESEND_API_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
)

REDACTED = "***REDACTED***"


class SecretRedactor(logging.Filter):
    """Scrub known env-var values from every log record before it is emitted.

    Loaded at startup so accidental `logger.exception()` on a `requests.HTTPError`
    or a `subprocess.CalledProcessError` cannot leak an API key into Actions logs.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = [
            v for k in SECRET_ENV_KEYS
            if (v := os.environ.get(k)) and len(v) >= 8
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            if s in msg:
                msg = msg.replace(s, REDACTED)
        record.msg = msg
        record.args = ()
        return True


def install_root_filter() -> None:
    """Install the redactor on the root logger. Idempotent."""
    root = logging.getLogger()
    if not any(isinstance(f, SecretRedactor) for f in root.filters):
        root.addFilter(SecretRedactor())
