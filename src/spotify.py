"""Spotify Web API client (client-credentials flow) for episode URL lookup.

We use this only to enrich an iTunes-discovered episode with the canonical
Spotify open URL, so the digest links open directly in Spotify for the user.

Auth: POST /api/token with `grant_type=client_credentials` + Basic auth.
Token TTL is 1 hour; cached per-process.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
USER_AGENT = "podcast-tracker/0.1"


class SpotifyAuthError(RuntimeError):
    pass


_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Fetch (or reuse cached) client-credentials token."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SpotifyAuthError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set")
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = await client.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise SpotifyAuthError(f"spotify token request failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    token = data["access_token"]
    ttl = int(data.get("expires_in") or 3600)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + ttl
    return token


async def lookup_episode(
    client: httpx.AsyncClient,
    *,
    podcast: str,
    episode_title: str,
    market: str = "US",
) -> str | None:
    """Search Spotify for an episode matching the (podcast, episode_title) pair.

    Returns the canonical https://open.spotify.com/episode/{id} URL, or None
    if no plausible match is found.
    """
    try:
        token = await _get_access_token(client)
    except SpotifyAuthError as e:
        logger.warning("spotify token unavailable: %s", e)
        return None

    # Spotify search is full-text; combining title + podcast usually pins the right episode.
    # Spotify rejects queries > 250 characters; truncate the title aggressively
    # since the podcast name is usually the more reliable disambiguator.
    SPOTIFY_QUERY_MAX = 200  # leave headroom under the 250-char limit
    title_budget = max(50, SPOTIFY_QUERY_MAX - len(podcast) - 1)
    truncated_title = (episode_title or "")[:title_budget].strip()
    query = f"{truncated_title} {podcast}".strip()
    resp = await client.get(
        SEARCH_URL,
        params={"q": query, "type": "episode", "limit": 5, "market": market},
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
        timeout=20.0,
    )
    if resp.status_code != 200:
        logger.warning("spotify search failed: %s %s", resp.status_code, resp.text[:200])
        return None

    items = (resp.json().get("episodes") or {}).get("items") or []
    if not items:
        return None

    # Best match: try to find one whose name is a strong substring overlap with episode_title.
    title_lower = (episode_title or "").lower().strip()
    for item in items:
        if not item:
            continue
        name = (item.get("name") or "").lower().strip()
        if name and (name in title_lower or title_lower in name):
            url = item.get("external_urls", {}).get("spotify")
            if url:
                return url
    # Fallback: first result
    first = items[0] if items[0] else None
    return (first or {}).get("external_urls", {}).get("spotify")


async def enrich_with_spotify_urls(
    episodes: list,
) -> None:
    """Enrich each EpisodeCandidate (inside Episode) in-place with a spotify_url field.

    No-op if SPOTIFY_CLIENT_ID is unset. Each lookup runs concurrently with a small
    semaphore so we stay polite to Spotify's API.
    """
    if not os.environ.get("SPOTIFY_CLIENT_ID"):
        return
    import asyncio
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient() as client:
        async def _one(ep) -> None:
            async with sem:
                c = ep.candidate
                if c.spotify_url:
                    return
                url = await lookup_episode(
                    client, podcast=c.podcast, episode_title=c.title,
                )
                if url:
                    c.spotify_url = url

        await asyncio.gather(*(_one(ep) for ep in episodes))
