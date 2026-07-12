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

## Usage
Paste a URL -> Run Audit -> View report / download PDF. History is kept in
the dashboard. Audits sample the homepage + up to N internal pages.

## Local development
```
pip install -r requirements.txt
GEO_PASSWORD=dev python app.py     # http://localhost:8080
```
