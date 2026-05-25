# Podcast Tracker — Progress Log

## 2026-05-23

- Project initialized at `~/projects/podcast-tracker/`.
- Brainstorm complete via `/ce-brainstorm`. Requirements doc at `spec/00_brainstorm.md`.
- Key decisions locked: GitHub Actions cron at 6am ET, named-people + company-name match scope, free transcript sources + Whisper.cpp fallback, daily digest email, ~15 key points per episode.
- Initial watchlist provided by Jon and folded into the requirements doc: 30 companies (with aliases), 39 named people (operators + investors/analysts), 3 specific podcasts.
- Implementation plan written via `/ce:plan` at `spec/01_plan.md`.
- Plan deepened via `/deepen-plan` with 10 parallel agents (6 research + 4 review). Plan rewritten in place at `spec/01_plan.md`. Headline outcomes:
  - **Factual corrections**: PodcastIndex `byterm` returns feeds not episodes (use `byperson` for both people and companies); Dwarkesh feed URL moved to Cloudflare Workers proxy; Buzzsprout feed ID for Everyday AI is `2175779`; private-repo runner is 2 vCPU + 8GB (not 4/16); `ffmpeg` is NOT pre-installed; `youtube-transcript-api` is IP-blocked on GHA (use yt-dlp captions); Anthropic Messages API has no public idempotency-key header.
  - **Security baked into Phase 1**: prompt-injection hardening (XML-tagged untrusted content + forced tool-use JSON), yt-dlp constraints (`--max-filesize 500M`, scheme/host allowlist, SSRF block), `permissions: contents: write` only, Jinja2 autoescape, secret-redaction log filter.
  - **Performance**: async discovery (httpx + semaphore) saves ~30s/run; async summarization (AsyncAnthropic + gather) saves ~3 min/run; value-weighted priority queue; 75-min total wallclock kill switch.
  - **Implementation quality**: `stefanzweifel/git-auto-commit-action@v7`, cron at `:05` past hour, `concurrency: cancel-in-progress: false`, prompt caching, `client.messages.stream()`, css-inline replaces premailer, Podcasting 2.0 `<podcast:transcript>` namespace as Tier 0.
- 6 open taste decisions surfaced in the plan; 4 resolved by Jon:
  - **Public repo** (4 vCPU + 16GB + unlimited Actions minutes)
  - **~10-file module split** as documented in "Repository layout"
  - **Pydantic + slim settings.yaml** kept (6 knobs in YAML, rest hardcoded)
  - **Warn-then-stop cost guard** ($5 warn / $10 hard-stop per run)
- Batch API and Phase 4 framing remain deferred.

## 2026-05-24

- Phase 1 foundation implemented and shipped on `feat/phase-1-foundation` branch.
- 47 unit tests pass; end-to-end smoke test passes (real digest email lands in Gmail).
- Pipeline runs in ~3 minutes wallclock: iTunes discovery → filter fast-path → Spotify enrichment → Whisper transcription (~20x realtime) → Sonnet summarization → Resend send.
- Discovery surface swapped: iTunes Search replaces PodcastIndex byperson (the latter only matches feeds with `<podcast:person>` tags, which is too sparse).
- Digest format finalized per Jon's spec: "Podcast Tracker (Month Day, Year) (N Episode[s])" heading, "## N. Podcast: Guests" per-episode header, Guests (plural list) / Company-role / Date / Link fields, "Summary" heading with concise bullets and no category labels.
- Spotify Web API integration added (`src/spotify.py`) with client-credentials flow and per-process token caching. Episode URLs in the digest now link directly to Spotify.
- Bug fixes during iteration: `extra={"message": ...}` collides with LogRecord's reserved field name (logging crash); Resend idempotency key needs to be caller-controlled so smoke tests are re-runnable without 24h waits.
- Phase 1 complete. Next: push branch, open PR, then Phase 2 (full watchlist async discovery + site-specific scrapers + GitHub Actions workflow).

## 2026-05-25

- PR #1 (Phase 1) squash-merged to main.
- Phase 3 work shipped on `feat/phase-3-production` → PR #2 open.
  - GitHub Actions cron workflow (dual UTC entries, ffmpeg install, Whisper model cache, secret env vars, git-auto-commit state push).
  - CI test workflow (pytest on push/PR).
  - Dependabot config (weekly pip + github-actions updates).
  - iTunes-backed daily discovery (fixing the PR #1 gap where `--daily` was still using PodcastIndex byperson).
  - iTunes concurrency → 3 with retry-on-403/429/5xx (Apple rate-limits at higher concurrency).
  - RSS feed fetch via httpx + certifi (fixes macOS dev-env SSL).
  - Filter pass batches 40 candidates/call (avoids overflowing Haiku on heavy days).
  - DiscoveryTotalFailure exception → distinct "discovery failed today" email; mark_ran_today NOT called so the next run retries.
- Local discovery dry-run on the full watchlist (7-day lookback) returned 198 candidates cleanly with zero 403s.
- Docs: `docs/resend-domain-setup.md` with step-by-step for verifying `send.jonkarolczak.com` (do before flipping cron on).
- 58 tests pass (was 47).
- Next: Jon to (a) verify Resend domain, (b) merge PR #2, (c) trigger first workflow_dispatch run to validate the daily path in production environment.
