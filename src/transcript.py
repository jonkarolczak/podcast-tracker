"""Transcript cascade: free sources first, Whisper fallback.

Tier 0 — Podcasting 2.0 <podcast:transcript> namespace (URL exposed by PodcastIndex)
Tier 1 — Site-specific scrapers (Phase 2 only; not built in Phase 1)
Tier 2 — YouTube auto-captions via yt-dlp --write-auto-subs
Tier 3 — faster-whisper (base.en int8 with podcast-tuned VAD)

Each tier catches its own exceptions and falls through. Whisper is budget-gated.

Security:
- yt-dlp invocations validate URL scheme/host and resolved-IP before running
- yt-dlp passes hard flags: --max-filesize 500M, --no-playlist, no-call-home, etc.
- Direct RSS enclosure downloads stream with byte cap to prevent disk fill
"""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import socket
import subprocess
import tempfile
import time
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

import httpx
import webvtt
from bs4 import BeautifulSoup

from .budget import WhisperBudget
from .models import EpisodeCandidate, Transcript

logger = logging.getLogger(__name__)


# --- Security: URL validation -----------------------------------------------

# We previously maintained a host allowlist, but podcast audio lives on dozens
# of CDNs and tracker domains (Buzzsprout's pscrb.fm, Megaphone's mgln.ai,
# Podroll's pdrl.fm, Spreaker, Ausha, Podtrac, etc.) and the allowlist became
# a maintenance burden that blocked legitimate downloads.
#
# Security model is now:
#   1. https only (no http/file/data/ftp)
#   2. Resolved IP must NOT be in private/metadata ranges (SSRF protection)
#   3. yt-dlp --max-filesize 500M caps disk DoS
#   4. The source URLs come from RSS/iTunes/PodcastIndex results, which
#      themselves are public podcast metadata services
# These three together cover the realistic threat surface for this use case.

_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class UnsafeUrlError(ValueError):
    pass


def validate_url(url: str) -> None:
    """Reject non-https URLs and URLs resolving to private/metadata IPs (SSRF guard).

    No host allowlist — podcast audio CDNs are too varied to enumerate.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError(f"non-https scheme: {parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeUrlError(f"dns failed for {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        for net in _BLOCKED_NETS:
            if ip in net:
                raise UnsafeUrlError(f"resolved ip {ip} in blocked range")


# --- Tier 0: Podcasting 2.0 transcript namespace ----------------------------

def _fetch_podcast_namespace_transcript(url: str, mime_type: str | None) -> str | None:
    validate_url(url)
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    text = resp.text
    mime = (mime_type or "").lower()
    if "vtt" in mime:
        captions = webvtt.from_buffer(StringIO(text))
        return " ".join(c.text.replace("\n", " ").strip() for c in captions if c.text.strip())
    if "subrip" in mime or "srt" in mime:
        return _strip_srt(text)
    if "html" in mime:
        return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return text


def _strip_srt(srt: str) -> str:
    """Crude SRT-to-text: drop index lines, timestamp lines, blank separators."""
    out: list[str] = []
    for line in srt.splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        out.append(s)
    return " ".join(out)


# --- Tier 2: YouTube captions via yt-dlp ------------------------------------

_YTDLP_BASE_ARGS = [
    "--max-filesize", "500M",
    "--no-playlist",
    "--no-call-home",
    "--no-update",
    "--socket-timeout", "30",
    "--retries", "2",
    "--no-warnings",
    "--restrict-filenames",
]


def _fetch_youtube_captions(video_url: str, workdir: Path, *, timeout: int = 60) -> str | None:
    validate_url(video_url)
    workdir.mkdir(parents=True, exist_ok=True)
    out_template = str(workdir / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        *_YTDLP_BASE_ARGS,
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en.*",
        "--sub-format", "vtt",
        "-o", out_template,
        video_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.warning("yt-dlp captions failed for %s: %s", video_url, result.stderr.strip()[:300])
        return None
    vtts = sorted(workdir.glob("*.vtt"))
    if not vtts:
        return None
    try:
        text = " ".join(
            c.text.replace("\n", " ").strip()
            for c in webvtt.read(str(vtts[0]))
            if c.text.strip()
        )
        return text or None
    finally:
        for f in vtts:
            f.unlink(missing_ok=True)


# --- Tier 3: faster-whisper -------------------------------------------------

_WHISPER_MODEL = None
_WHISPER_CFG: dict | None = None


def _get_whisper_model(model_name: str, compute_type: str, cpu_threads: int):
    global _WHISPER_MODEL, _WHISPER_CFG
    cfg = {"model": model_name, "compute_type": compute_type, "threads": cpu_threads}
    if _WHISPER_MODEL is not None and _WHISPER_CFG == cfg:
        return _WHISPER_MODEL
    from faster_whisper import WhisperModel  # heavy import; defer
    logger.info("loading whisper model", extra=cfg)
    _WHISPER_MODEL = WhisperModel(
        model_name,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        num_workers=1,
    )
    _WHISPER_CFG = cfg
    return _WHISPER_MODEL


def _download_audio(audio_url: str, workdir: Path, *, timeout: int = 300) -> Path:
    """For YouTube URLs use yt-dlp; for direct RSS enclosure URLs stream with requests."""
    validate_url(audio_url)
    workdir.mkdir(parents=True, exist_ok=True)
    if "youtube.com" in audio_url or "youtu.be" in audio_url:
        out_template = str(workdir / "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            *_YTDLP_BASE_ARGS,
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "5",
            "-o", out_template,
            audio_url,
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        mp3s = sorted(workdir.glob("*.mp3"))
        if not mp3s:
            raise RuntimeError("yt-dlp succeeded but produced no mp3")
        return mp3s[0]
    # Direct RSS enclosure URL — stream with byte cap.
    # Some hosts (Substack) 403 bot-like User-Agents; use a Chrome-flavored
    # UA so audio fetches succeed.
    ext = Path(urlparse(audio_url).path).suffix or ".mp3"
    out_path = workdir / f"episode{ext}"
    max_bytes = 500 * 1024 * 1024  # 500 MB cap
    bytes_written = 0
    browser_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    )
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": browser_ua},
    ) as client:
        with client.stream("GET", audio_url) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 16):
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if bytes_written > max_bytes:
                        raise RuntimeError(f"audio download exceeded {max_bytes} bytes")
    return out_path


def _transcribe_with_whisper(
    audio_path: Path,
    model_name: str,
    compute_type: str,
    cpu_threads: int,
) -> tuple[str, float]:
    model = _get_whisper_model(model_name, compute_type, cpu_threads)
    segments, _info = model.transcribe(
        str(audio_path),
        language="en",
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=1000,
            min_speech_duration_ms=250,
            speech_pad_ms=400,
        ),
        condition_on_previous_text=True,
    )
    segments = list(segments)
    text = " ".join(s.text.strip() for s in segments if s.text.strip())
    if segments:
        avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
        confidence = math.exp(avg_logprob)
    else:
        confidence = 0.0
    return text, confidence


# --- Public cascade ---------------------------------------------------------

def fetch_transcript(
    candidate: EpisodeCandidate,
    *,
    whisper_budget: WhisperBudget,
    whisper_model: str = "base.en",
    whisper_compute_type: str = "int8",
    whisper_threads: int = 4,
) -> Transcript:
    """Run the cascade. Each tier catches its own exceptions and falls through."""

    # Tier 0
    if candidate.podcast_transcript_url:
        try:
            text = _fetch_podcast_namespace_transcript(
                candidate.podcast_transcript_url,
                candidate.podcast_transcript_type,
            )
            if text and text.strip():
                logger.info("transcript via namespace", extra={"guid": candidate.guid})
                return Transcript(text=text, source="podcast_namespace")
        except Exception as e:
            logger.warning("namespace tier failed for %s: %s", candidate.guid, e)

    # Tier 2
    if candidate.youtube_url:
        try:
            with tempfile.TemporaryDirectory(prefix="captions-") as td:
                text = _fetch_youtube_captions(candidate.youtube_url, Path(td))
            if text and text.strip():
                logger.info("transcript via youtube captions", extra={"guid": candidate.guid})
                return Transcript(text=text, source="youtube_captions")
        except Exception as e:
            logger.warning("captions tier failed for %s: %s", candidate.guid, e)

    # Tier 3 — Whisper (budget-gated)
    if not candidate.audio_url:
        logger.info("no audio_url for %s; marking unavailable", candidate.guid)
        return Transcript(text="", source="unavailable", confidence=0.0)
    if not whisper_budget.can_afford(candidate.duration_minutes):
        logger.info(
            "whisper budget exhausted for %s (%.1f min remaining, episode %.1f min)",
            candidate.guid, whisper_budget.minutes_remaining, candidate.duration_minutes,
        )
        return Transcript(text="", source="unavailable", confidence=0.0)

    try:
        with tempfile.TemporaryDirectory(prefix="whisper-") as td:
            audio_path = _download_audio(candidate.audio_url, Path(td))
            try:
                start = time.monotonic()
                text, confidence = _transcribe_with_whisper(
                    audio_path, whisper_model, whisper_compute_type, whisper_threads,
                )
                wallclock_min = (time.monotonic() - start) / 60
                whisper_budget.spend(wallclock_min)
                logger.info(
                    "transcript via whisper",
                    extra={
                        "guid": candidate.guid,
                        "wallclock_min": round(wallclock_min, 2),
                        "confidence": round(confidence, 3),
                    },
                )
                return Transcript(text=text, source="whisper", confidence=confidence)
            finally:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
    except Exception as e:
        logger.error("whisper tier failed for %s: %s", candidate.guid, e)
        return Transcript(text="", source="unavailable", confidence=0.0)
