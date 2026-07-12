#!/usr/bin/env python3
"""
Prospecting engine for the GEO Audit dashboard.

Pipeline: Google Places search -> audit prospect sites -> harvest contact email
-> LLM-generated personalised outreach -> draft or (capped, opt-in) auto-send.

All configuration comes from environment variables; see README.
"""

import json
import re
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ----------------------------------------------------------------------------
# Google Places (New) - Text Search
# ----------------------------------------------------------------------------

def search_places(query, api_key, max_results=20):
    """Search businesses via Places API (New) Text Search.

    Returns list of dicts: {place_id, name, website, address}.
    Only businesses WITH a website are returned (no site = nothing to audit).
    Google caps any text search at 60 results (20/page x 3 pages).
    """
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # FieldMask keeps us in the cheapest tier that includes websiteUri
        "X-Goog-FieldMask": ("places.id,places.displayName,places.websiteUri,"
                             "places.formattedAddress,nextPageToken"),
    }
    out, token = [], None
    while len(out) < min(max_results, 60):
        body = {"textQuery": query, "pageSize": 20}
        if token:
            body["pageToken"] = token
        r = requests.post(url, headers=headers, json=body, timeout=20)
        if not r.ok:
            raise RuntimeError(f"Places API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        for p in data.get("places", []):
            site = p.get("websiteUri")
            if not site:
                continue
            out.append({
                "place_id": p.get("id", ""),
                "name": (p.get("displayName") or {}).get("text", "Unknown"),
                "website": site,
                "address": p.get("formattedAddress", ""),
            })
        token = data.get("nextPageToken")
        if not token:
            break
    return out[:max_results]


# ----------------------------------------------------------------------------
# Contact email harvesting (from the prospect's own site)
# ----------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
BAD_EMAIL_HINTS = ("example.", "sentry", "wixpress", "@2x", ".png", ".jpg",
                   "yourdomain", "email.com", "domain.com", "@sentry")


def harvest_email(base_url, session=None):
    """Fetch homepage + a contact-ish page and extract the best email found.

    Places API does not return email addresses, so we look on the site itself.
    Returns an email string or None.
    """
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", UA)
    parsed = urlparse(base_url if base_url.startswith("http") else "https://" + base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    pages, found = [base + "/"], []
    try:
        r = s.get(base + "/", timeout=15)
        if r.ok:
            found += EMAIL_RE.findall(r.text)
            for m in re.finditer(r'href=["\']([^"\']*contact[^"\']*)["\']', r.text, re.I):
                pages.append(urljoin(base + "/", m.group(1)))
                break
    except requests.RequestException:
        return None
    for p in pages[1:2]:
        try:
            r = s.get(p, timeout=15)
            if r.ok:
                found += EMAIL_RE.findall(r.text)
        except requests.RequestException:
            pass

    domain = parsed.netloc.removeprefix("www.")
    cleaned = []
    for e in found:
        e = e.strip().strip(".").lower()
        if any(b in e for b in BAD_EMAIL_HINTS):
            continue
        cleaned.append(e)
    if not cleaned:
        return None
    # prefer an address on the business's own domain
    own = [e for e in cleaned if e.endswith("@" + domain) or e.endswith("." + domain)]
    return (own or cleaned)[0]


# ----------------------------------------------------------------------------
# LLM outreach generation (OpenAI / Gemini / Anthropic)
# ----------------------------------------------------------------------------

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-haiku-4-5",
}

PROMPT = """You write short, honest cold outreach emails for a web consultant.

Business: {name} ({address})
Website: {website}
Their AI Visibility score: {score}/100 (grade {grade})
Top issues found (plain language):
{issues}

Sender: {sender_name} from {brand}.

Write an email to this business owner. Rules:
- 90 to 140 words in the body. No fluff, no hype, no exclamation marks.
- Open with ONE specific finding from the list above, stated plainly, so they
  can tell this is not a mass template.
- Briefly explain the stake: customers increasingly ask ChatGPT and Google's AI
  for recommendations, and these issues affect whether the business gets named.
- Mention we ran a free automated check and have a full report ready.
- Single call to action: reply and we'll send the full report, no obligation.
- Do NOT invent facts, statistics, or claims not in the findings above.
- No placeholder brackets. Sign off with the sender's name only.

Return ONLY valid JSON, no markdown fences: {{"subject": "...", "body": "..."}}
The subject must be under 60 characters, specific, not clickbait."""


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in LLM response")
    data = json.loads(m.group(0))
    if not data.get("subject") or not data.get("body"):
        raise ValueError("LLM JSON missing subject/body")
    return {"subject": str(data["subject"])[:150], "body": str(data["body"])}


def generate_email(provider, api_key, context, model=None):
    """context: dict with name, address, website, score, grade, issues (list of str),
    sender_name, brand. Returns {"subject","body"}."""
    provider = (provider or "").lower()
    model = model or DEFAULT_MODELS.get(provider)
    if provider not in DEFAULT_MODELS:
        raise RuntimeError(f"LLM_PROVIDER must be one of {list(DEFAULT_MODELS)}")

    prompt = PROMPT.format(
        name=context["name"], address=context.get("address", ""),
        website=context["website"], score=context["score"], grade=context["grade"],
        issues="\n".join(f"- {i}" for i in context["issues"][:5]) or "- (none)",
        sender_name=context["sender_name"], brand=context["brand"])

    if provider == "openai":
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45)
        if not r.ok:
            raise RuntimeError(f"OpenAI error {r.status_code}: {r.text[:300]}")
        text = r.json()["choices"][0]["message"]["content"]

    elif provider == "gemini":
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=45)
        if not r.ok:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]

    else:  # anthropic
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={"model": model, "max_tokens": 500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=45)
        if not r.ok:
            raise RuntimeError(f"Anthropic error {r.status_code}: {r.text[:300]}")
        text = r.json()["content"][0]["text"]

    return _extract_json(text)


# ----------------------------------------------------------------------------
# SMTP sending (with compliance footer)
# ----------------------------------------------------------------------------

def compliance_footer(sender_name, brand, physical_address):
    lines = ["", "--", f"{sender_name} | {brand}"]
    if physical_address:
        lines.append(physical_address)
    lines.append("If you'd prefer not to hear from me, just reply "
                 "\"unsubscribe\" and I won't contact you again.")
    return "\n".join(lines)


def send_email(cfg, to_addr, subject, body):
    """cfg: dict with host, port, user, password, from_addr, sender_name.
    Raises on failure; returns True on success."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.get("sender_name") or cfg["from_addr"],
                              cfg["from_addr"]))
    msg["To"] = to_addr
    with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)), timeout=30) as s:
        s.ehlo()
        if int(cfg.get("port", 587)) != 465:
            s.starttls()
            s.ehlo()
        if cfg.get("user"):
            s.login(cfg["user"], cfg["password"])
        s.sendmail(cfg["from_addr"], [to_addr], msg.as_string())
    return True


def top_issues_from_json(report_json_path, limit=5):
    """Pull the highest-impact failing checks from a stored audit JSON,
    phrased for a non-technical reader."""
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    with open(report_json_path, encoding="utf-8") as f:
        raw = json.load(f)
    issues = [c for c in raw["checks"] if c["status"] != "pass"]
    issues.sort(key=lambda c: (order.get(c.get("impact", "medium"), 2),
                               -c["max_points"]))
    out = []
    for c in issues[:limit]:
        why = c.get("why") or c["name"]
        out.append(f"{c['name']}: {why}")
    return out, raw["score"], raw["grade"]
