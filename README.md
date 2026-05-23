# podcast-tracker

Daily-scheduled agent that monitors podcasts for new episodes featuring a curated watchlist of AI/finance operators, investors, and companies, transcribes the matches, and delivers a structured ~15-point summary to a single email digest every morning.

Runs on GitHub Actions, laptop-independent.

## Status

In planning. See [`spec/00_brainstorm.md`](spec/00_brainstorm.md) for the requirements doc and [`spec/01_plan.md`](spec/01_plan.md) for the implementation plan. [`progress.md`](progress.md) tracks running progress.

## How it works (planned)

1. **Discover** new episodes via PodcastIndex `byperson` (39 named people + 30 company aliases), direct RSS for 3 specific podcasts (Dwarkesh, Invest Like the Best, Everyday AI), and Exa as a fallback.
2. **Filter** candidates with Claude Haiku 4.5 to drop mentions vs actual guest appearances.
3. **Transcribe** via a 4-tier cascade: Podcasting 2.0 namespace tag → site-specific scrape → YouTube auto-captions (via yt-dlp) → faster-whisper fallback.
4. **Summarize** with Claude Sonnet 4.6 into ~15 structured key points with category tags and per-point evidence.
5. **Render** an HTML + plain-text digest with Jinja2 + css-inline.
6. **Send** via Resend with idempotency key.
7. **Commit** updated state JSON back to `main` via GitHub Actions.

## Stack

- Python 3.13
- `anthropic`, `exa-py`, `feedparser`, `httpx`, `yt-dlp`, `faster-whisper`, `resend`, `jinja2`, `css-inline`, `webvtt-py`, `beautifulsoup4`, `pyyaml`, `pydantic`
- GitHub Actions cron (`:05` past hour, dual UTC for DST), `stefanzweifel/git-auto-commit-action@v7` for state commits

## Setup

(Implementation has not started yet — these instructions are forward-looking.)

```bash
git clone https://github.com/<owner>/podcast-tracker.git
cd podcast-tracker
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
python -m src.tracker --once <test_episode_url>   # smoke test
```

Required GitHub Actions secrets (same names as `.env.example`):
- `ANTHROPIC_API_KEY`
- `EXA_API_KEY`
- `PODCASTINDEX_API_KEY`
- `PODCASTINDEX_API_SECRET`
- `RESEND_API_KEY`
- `DIGEST_FROM_EMAIL`
- `DIGEST_TO_EMAIL`

## License

Personal use. No license granted.
