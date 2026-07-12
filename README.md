# GEO Audit Dashboard

Password-protected web dashboard that audits any website for AI visibility
(ChatGPT, Claude, Perplexity, Google AI Overviews) and generates branded
PDF reports. Built for deployment on Coolify.

## Deploy on Coolify

1. Push this repo to GitHub/GitLab.
2. Coolify -> **+ New** -> **Application** -> pick the repo.
   Build pack: **Dockerfile** (auto-detected). Port: **8080**.
3. **Environment Variables** -> add:
   - `GEO_PASSWORD` = your login password (required - app won't start without it)
   - `GEO_BRAND`    = default brand name on reports (optional)
4. **Storages** -> Add **Volume Mount**: destination path `/app/data`
   (keeps reports + history across redeploys - without this they're wiped
   every deploy).
5. Optionally attach a domain in **Domains** - Coolify issues HTTPS
   automatically.
6. **Deploy**. Open the app URL, log in with your password.

## Prospecting (optional - each feature enables when its vars are set)

The Prospecting tab finds businesses, audits their sites, harvests a contact
email from their pages, and drafts personalised outreach from the actual
audit findings.

| Variable | Purpose |
|---|---|
| `GOOGLE_PLACES_API_KEY` | Enables business search (Places API New, Text Search). ~5,000 free calls/month; each search costs 1-3 calls. |
| `LLM_PROVIDER` | `openai`, `gemini`, or `anthropic` - enables email drafting |
| `LLM_API_KEY` | API key for the chosen provider |
| `LLM_MODEL` | Optional model override (defaults: gpt-4o-mini / gemini-2.0-flash / claude-haiku-4-5) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | Enables sending. Works with any SMTP (Brevo, Gmail app password, etc.) |
| `OUTREACH_SENDER_NAME` | Name signed on emails |
| `OUTREACH_ADDRESS` | Physical address appended to the footer (required by anti-spam law in most countries) |
| `AUTO_SEND` | `1` to allow auto-sending after drafting. Default `0` = drafts only. Even at `1`, each batch has its own checkbox. |
| `SEND_DAILY_CAP` | Max emails sent per day (default 10). Applies to auto and manual sends. |

Every sent email automatically gets a footer with your name, brand, physical
address, and an unsubscribe line. Start with drafts only, review the first
batch by hand, and keep the daily cap low - cold outreach deliverability
depends on volume discipline, and anti-spam law (CAN-SPAM, GDPR, Australia's
Spam Act) applies to you, not the tool.

## Usage
Paste a URL -> Run Audit -> View report / download PDF. History is kept in
the dashboard. Audits sample the homepage + up to N internal pages.

## Local development
```
pip install -r requirements.txt
GEO_PASSWORD=dev python app.py     # http://localhost:8080
```
