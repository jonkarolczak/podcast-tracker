# Resend domain verification

Phase 1 sends from the shared `onboarding@resend.dev` address. Gmail accepts those for low-volume personal use, but it's marginal — the first few mails sometimes hit Spam, the From: address looks generic, and Gmail's bulk-sender filters give shared-domain mail less benefit of the doubt over time.

Before flipping the daily cron on for production, switch `DIGEST_FROM_EMAIL` to a verified subdomain of `jonkarolczak.com`. Roughly 10 minutes including DNS propagation wait.

## Steps

### 1. Add the domain in Resend

1. Go to https://resend.com/domains
2. Click **Add Domain**
3. Enter `send.jonkarolczak.com` (a subdomain, not the apex — keeps reputation isolated and doesn't interfere with anything else routing to `jonkarolczak.com`).
4. Region: pick the closest one (likely **us-east-1** if you're in Nashville).

Resend will show you 4 DNS records to add:
- 1 × `MX` record (for bounce / complaint feedback)
- 1 × `TXT` record (SPF)
- 1 × `TXT` record (DKIM, long public key)
- 1 × `TXT` record (DMARC, optional but recommended)

### 2. Add the DNS records at Vercel

Your domain's nameservers are at Vercel (`ns1.vercel-dns.com`), so DNS records go there — not Porkbun.

1. https://vercel.com/dashboard → **Domains** → `jonkarolczak.com` → **DNS Records**
2. Add each record from Resend's dashboard:
   - **MX**: Name = `send`, Value = `feedback-smtp.<region>.amazonses.com`, Priority = 10, TTL 60
   - **TXT (SPF)**: Name = `send`, Value = `v=spf1 include:amazonses.com ~all`, TTL 60
   - **TXT (DKIM)**: Name = `resend._domainkey.send`, Value = the long key Resend gives you (paste exactly), TTL 60
   - **TXT (DMARC)**: Name = `_dmarc`, Value = `v=DMARC1; p=none; rua=mailto:jonkarolczak@gmail.com; fo=1; aspf=r; adkim=r`, TTL 60

3. Save each.

### 3. Verify in Resend

1. Back at https://resend.com/domains, click the domain → **Verify DNS records**
2. Usually verifies within 1–5 minutes; can take up to an hour for global DNS propagation.
3. Once verified, all four checkmarks turn green.

### 4. Switch the From address

In `.env` (local) and in GitHub Actions secrets:

```
DIGEST_FROM_EMAIL=Podcast Tracker <digest@send.jonkarolczak.com>
```

The local-part (`digest@`) can be anything — Resend doesn't require it to exist as a mailbox. You don't need to set up a mail server.

Update the GitHub Actions secret too:
- https://github.com/jonkarolczak/podcast-tracker/settings/secrets/actions
- Edit `DIGEST_FROM_EMAIL` → paste the new value

### 5. Verify one send

Run a smoke test locally:

```bash
source venv/bin/activate
python -m src.tracker preview
```

Confirm the email arrives in your Gmail Inbox (not Spam) and the From: header shows `digest@send.jonkarolczak.com`.

### 6. DMARC progression (optional, Phase 4)

After 2–4 weeks of clean reports at `p=none`, you can step up to `p=quarantine` then `p=reject` to harden the domain against spoofing. For a single-recipient personal digest the upside is small; `p=none` is fine indefinitely.

## Why not just use Gmail SMTP?

Gmail's app-password mechanism is being deprecated and rate-limited, and the same DNS authentication is required regardless. Resend's purpose-built transactional sender, free 3000/month tier, and clean dashboard make it the lower-friction choice.
