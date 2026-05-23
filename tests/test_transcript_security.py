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


def test_host_not_in_allowlist():
    with pytest.raises(UnsafeUrlError):
        validate_url("https://evil.example.com/x.mp3")


def test_youtube_allowed():
    # Will reach DNS lookup, which should succeed and resolve to a public IP.
    # If offline this fails for a different reason — keep loose assertion.
    try:
        validate_url("https://www.youtube.com/watch?v=abc")
    except UnsafeUrlError as e:
        # Only acceptable failure here is DNS / network — not allowlist
        assert "allowlist" not in str(e) and "scheme" not in str(e)


def test_known_podcast_substring_allowed_in_host_check():
    # We can't always reach DNS for arbitrary podcast hosts in unit tests; just confirm
    # the host substring check accepts known patterns before DNS resolution.
    from src.transcript import _is_known_podcast_host
    assert _is_known_podcast_host("feeds.megaphone.fm")
    assert _is_known_podcast_host("rss.buzzsprout.com")
    assert _is_known_podcast_host("apple.dwarkesh-podcast.workers.dev")
    assert not _is_known_podcast_host("malicious.example.com")
