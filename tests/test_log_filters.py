"""SecretRedactor must scrub env-var values from log records."""
import logging
import os

import pytest

from src.log_filters import REDACTED, SecretRedactor, install_root_filter


@pytest.fixture
def secret_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdef0123456789")
    monkeypatch.setenv("RESEND_API_KEY", "re_topsecretvalue999")
    monkeypatch.setenv("PODCASTINDEX_API_SECRET", "very-secret-thing")
    yield


def test_redacts_anthropic_key(secret_env, caplog):
    install_root_filter()
    with caplog.at_level(logging.WARNING):
        logging.warning("auth failed using key sk-ant-abcdef0123456789 oh no")
    assert "sk-ant-abcdef0123456789" not in caplog.text
    assert REDACTED in caplog.text


def test_redacts_multiple_secrets(secret_env, caplog):
    install_root_filter()
    with caplog.at_level(logging.INFO):
        logging.info("sending with re_topsecretvalue999 and very-secret-thing")
    assert "re_topsecretvalue999" not in caplog.text
    assert "very-secret-thing" not in caplog.text


def test_install_is_idempotent(secret_env):
    root = logging.getLogger()
    initial = len(root.filters)
    install_root_filter()
    install_root_filter()
    install_root_filter()
    redactors = [f for f in root.filters if isinstance(f, SecretRedactor)]
    assert len(redactors) == 1


def test_does_not_scrub_short_strings(monkeypatch, caplog):
    """Short env values are not used as redaction patterns (false-positive avoidance)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "abc")  # below 8-char threshold
    f = SecretRedactor()
    record = logging.LogRecord("test", logging.INFO, "x", 1, "abc def", None, None)
    f.filter(record)
    assert "abc" in record.msg
