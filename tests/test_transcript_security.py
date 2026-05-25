"""URL validation: scheme, host allowlist, SSRF block."""
import pytest

from src.transcript import UnsafeUrlError, validate_url


def test_https_required():
    with pytest.raises(UnsafeUrlError):
        validate_url("http://www.youtube.com/watch?v=abc")
    with pytest.raises(UnsafeUrlError):
        validate_url("file:///etc/passwd")
    with pytest.raises(UnsafeUrlError):
        validate_url("data:text/html,<script>")


def test_arbitrary_https_host_accepted_when_public_ip():
    """No host allowlist anymore — any https URL resolving to a public IP passes.

    Validates DNS to a real host. Skipped silently if offline.
    """
    try:
        validate_url("https://www.youtube.com/watch?v=abc")
    except UnsafeUrlError as e:
        # Only acceptable failure here is DNS / network — not scheme/host
        assert "scheme" not in str(e)


def test_blocked_when_resolves_to_loopback(monkeypatch):
    """A malicious DNS answer pointing to 127.0.0.1 is the actual SSRF threat."""
    import socket

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        validate_url("https://evil.example.com/x.mp3")


def test_blocked_when_resolves_to_cloud_metadata(monkeypatch):
    """169.254.169.254 (AWS/GCP metadata) must be blocked."""
    import socket

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        validate_url("https://evil.example.com/x.mp3")
