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
- Plan is now implementation-ready. Next: `/ce:work` to begin Phase 1 (end-to-end vertical slice + security baseline + smoke test email).
