#!/usr/bin/env python3
"""
AI Visibility Audit (GEO Audit Tool)
====================================
Scans a website and scores how visible/citable it is to AI answer engines
(ChatGPT, Claude, Perplexity, Google AI Overviews).

Generates a branded HTML + PDF report you can deliver to clients.

Usage:
    python geo_audit.py https://example.com
    python geo_audit.py https://example.com --brand "Your Agency" --max-pages 8 --out reports/example

Outputs:
    <out>.html   - branded report (always)
    <out>.pdf    - PDF version (if weasyprint installed)
    <out>.json   - raw check data (for your records / automation)

Dependencies:
    pip install requests beautifulsoup4        (required)
    pip install weasyprint                     (optional, for PDF)
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 GEO-Audit/1.0")

TIMEOUT = 15

# AI crawlers that matter in 2026. (bot token as it appears in robots.txt)
AI_BOTS = {
    "GPTBot":          "OpenAI - training + retrieval",
    "OAI-SearchBot":   "OpenAI - ChatGPT search index",
    "ChatGPT-User":    "OpenAI - live browsing on user request",
    "ClaudeBot":       "Anthropic - Claude crawling",
    "Claude-User":     "Anthropic - Claude live browsing",
    "PerplexityBot":   "Perplexity - search index",
    "Perplexity-User": "Perplexity - live browsing",
    "Google-Extended": "Google - Gemini/AI training",
    "CCBot":           "Common Crawl - feeds many AI models",
}

AUTHORITATIVE_HINTS = (
    ".gov", ".edu", "wikipedia.org", "nih.gov", "who.int", "reuters.com",
    "nature.com", "sciencedirect.com", "statista.com", "pewresearch.org",
    "gartner.com", "forbes.com", "harvard.edu", "bbc.", "nytimes.com",
)

CURRENT_YEAR = datetime.now().year


# ----------------------------------------------------------------------------
# Fetch helpers
# ----------------------------------------------------------------------------

def fetch(url, session):
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        return r
    except requests.RequestException as e:
        print(f"  [warn] fetch failed: {url} ({e.__class__.__name__})")
        return None


def get_soup(resp):
    if resp is None or not resp.ok:
        return None
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype:
        return None
    return BeautifulSoup(resp.text, "html.parser")


def same_domain(url, root_netloc):
    n = urlparse(url).netloc.lower().removeprefix("www.")
    return n == root_netloc


def discover_pages(home_url, soup, root_netloc, max_pages):
    """Pick a sample of internal pages, preferring content-rich ones."""
    if soup is None:
        return []
    seen, picked = set(), []
    priority, normal = [], []
    for a in soup.find_all("a", href=True):
        href = urljoin(home_url, a["href"].split("#")[0])
        p = urlparse(href)
        if p.scheme not in ("http", "https") or not same_domain(href, root_netloc):
            continue
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|zip|mp4|webp|svg)$", p.path, re.I):
            continue
        if href in seen or href.rstrip("/") == home_url.rstrip("/"):
            continue
        seen.add(href)
        # prefer pages likely to be cited: blog posts, services, FAQs, about
        if re.search(r"(blog|news|article|faq|service|about|guide|how|what|why)", p.path, re.I):
            priority.append(href)
        else:
            normal.append(href)
    for href in priority + normal:
        if len(picked) >= max_pages:
            break
        picked.append(href)
    return picked


# ----------------------------------------------------------------------------
# Check framework
# ----------------------------------------------------------------------------

class Check:
    def __init__(self, category, name, status, points, max_points, detail, fix):
        self.category = category      # str
        self.name = name              # str
        self.status = status          # "pass" | "warn" | "fail"
        self.points = points          # earned
        self.max_points = max_points  # possible
        self.detail = detail          # what we found
        self.fix = fix                # recommendation if not passing

    def as_dict(self):
        return self.__dict__.copy()


def make_check(checks, category, name, ok, max_points, detail_pass, detail_fail, fix,
               warn=False):
    """Append a pass/warn/fail check. warn=True gives half points."""
    if ok:
        checks.append(Check(category, name, "pass", max_points, max_points,
                            detail_pass, ""))
    elif warn:
        checks.append(Check(category, name, "warn", max_points // 2, max_points,
                            detail_fail, fix))
    else:
        checks.append(Check(category, name, "fail", 0, max_points,
                            detail_fail, fix))


# ----------------------------------------------------------------------------
# Category A: AI Crawler Access
# ----------------------------------------------------------------------------

def robots_verdicts(robots_text):
    """Parse robots.txt into per-AI-bot allow/block verdicts.

    Simplified model: a bot is 'blocked' if its own UA group (or the * group,
    when no specific group exists) contains 'Disallow: /'.
    """
    groups = {}            # lowercase agent -> list of (directive, value)
    current_agents = []
    last_was_agent = False  # consecutive user-agent lines share one group
    for raw in robots_text.splitlines():
        line = raw.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if not last_was_agent:      # a new group starts after any directive
                current_agents = []
            agent = val.lower()
            groups.setdefault(agent, [])
            current_agents.append(agent)
            last_was_agent = True
        elif key in ("disallow", "allow"):
            for a in current_agents:
                groups[a].append((key, val))
            last_was_agent = False
        else:                           # sitemap, crawl-delay, etc.
            last_was_agent = False

    def blocked(agent_lc):
        rules = groups.get(agent_lc)
        if rules is None:
            rules = groups.get("*", [])
        dis_all = any(d == "disallow" and v == "/" for d, v in rules)
        allow_all = any(d == "allow" and (v == "/" or v == "") for d, v in rules)
        return dis_all and not allow_all

    verdicts = {}
    for bot in AI_BOTS:
        lc = bot.lower()
        explicit = lc in groups
        verdicts[bot] = {
            "blocked": blocked(lc),
            "explicit_rule": explicit,
        }
    return verdicts


def check_crawler_access(base, session, checks, data):
    CAT = "AI Crawler Access"

    # robots.txt
    r = fetch(urljoin(base, "/robots.txt"), session)
    robots_text = r.text if (r is not None and r.ok and len(r.text) < 500_000) else ""
    data["robots_found"] = bool(robots_text)

    if robots_text:
        verdicts = robots_verdicts(robots_text)
        blocked = [b for b, v in verdicts.items() if v["blocked"]]
        data["ai_bots_blocked"] = blocked
        make_check(
            checks, CAT, "AI crawlers allowed in robots.txt",
            ok=len(blocked) == 0, max_points=12,
            detail_pass="No AI crawlers (GPTBot, ClaudeBot, PerplexityBot, etc.) are blocked.",
            detail_fail=f"Blocked AI crawlers: {', '.join(blocked)}. "
                        f"These engines cannot index the site and will never cite it.",
            fix="Update robots.txt to allow AI crawlers you want citations from "
                "(GPTBot, OAI-SearchBot, ClaudeBot, PerplexityBot, Google-Extended).",
            warn=0 < len(blocked) <= 2,
        )
    else:
        make_check(
            checks, CAT, "robots.txt reachable",
            ok=False, max_points=12, warn=True,
            detail_pass="",
            detail_fail="robots.txt missing or unreadable. Crawlers fall back to "
                        "defaults, but you lose control and the sitemap hint.",
            fix="Add a robots.txt that explicitly allows AI crawlers and points to the sitemap.",
        )

    # Cloudflare / bot-management heuristic
    home = data.get("_home_resp")
    cf = bool(home is not None and ("cf-ray" in home.headers or
              "cloudflare" in home.headers.get("server", "").lower()))
    data["cloudflare_detected"] = cf
    if cf:
        make_check(
            checks, CAT, "CDN bot-blocking risk (Cloudflare)",
            ok=False, warn=True, max_points=4,
            detail_pass="",
            detail_fail="Site is behind Cloudflare. Cloudflare's newer defaults can "
                        "block AI crawlers at the network level even when robots.txt allows them.",
            fix="In the Cloudflare dashboard, review AI-bot / bot-fight settings and "
                "explicitly allow desired AI crawlers.",
        )
    else:
        make_check(checks, CAT, "CDN bot-blocking risk", ok=True, max_points=4,
                   detail_pass="No CDN-level AI-bot blocking detected.",
                   detail_fail="", fix="")

    # llms.txt
    r = fetch(urljoin(base, "/llms.txt"), session)
    has_llms = bool(r is not None and r.ok and
                    "html" not in r.headers.get("content-type", "") and r.text.strip())
    # some servers return the 404 page as HTML with 200; the content-type guard covers most
    data["llms_txt"] = has_llms
    make_check(
        checks, CAT, "llms.txt present",
        ok=has_llms, max_points=4, warn=not has_llms,  # emerging standard: warn, not fail
        detail_pass="llms.txt found - gives AI systems a curated map of key content.",
        detail_fail="No llms.txt. This emerging standard lets you hand AI engines a "
                    "clean, prioritized index of your most important pages.",
        fix="Publish /llms.txt listing the site's key pages with one-line descriptions.",
    )

    # sitemap
    sm = fetch(urljoin(base, "/sitemap.xml"), session)
    has_sm = bool(sm is not None and sm.ok and "<urlset" in sm.text[:2000] or
                  (sm is not None and sm.ok and "<sitemapindex" in sm.text[:2000]))
    if not has_sm and robots_text:
        m = re.search(r"(?im)^sitemap:\s*(\S+)", robots_text)
        if m:
            sm2 = fetch(m.group(1), session)
            has_sm = bool(sm2 is not None and sm2.ok)
    data["sitemap"] = has_sm
    make_check(
        checks, CAT, "XML sitemap present",
        ok=has_sm, max_points=5,
        detail_pass="Sitemap found - retrieval engines can discover all pages.",
        detail_fail="No sitemap.xml found. AI search engines rely on the same "
                    "indexes as classic search; undiscovered pages can't be cited.",
        fix="Generate an XML sitemap (Yoast/RankMath on WordPress) and reference it in robots.txt.",
    )

    # Text-to-HTML ratio on homepage (JS-rendered content risk)
    if home is not None and home.ok:
        soup = BeautifulSoup(home.text, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        visible = len(soup.get_text(" ", strip=True))
        total = max(len(home.text), 1)
        ratio = visible / total
        data["text_html_ratio"] = round(ratio, 3)
        make_check(
            checks, CAT, "Content readable without JavaScript",
            ok=ratio >= 0.10 and visible > 800,
            warn=ratio >= 0.04 and visible > 300,
            max_points=5,
            detail_pass=f"Good text-to-HTML ratio ({ratio:.0%}); content is server-rendered.",
            detail_fail=f"Very low visible text ({visible} chars, ratio {ratio:.0%}). "
                        "Most AI crawlers do not execute JavaScript - JS-only content is invisible to them.",
            fix="Ensure critical content is server-side rendered (SSR) or pre-rendered.",
        )


# ----------------------------------------------------------------------------
# Category B: Structured Data & Machine Readability
# ----------------------------------------------------------------------------

def extract_jsonld_types(soups):
    types = Counter()
    for soup in soups:
        if soup is None:
            continue
        for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
            try:
                payload = json.loads(tag.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if not isinstance(item, dict):
                    continue
                graph = item.get("@graph", [item])
                for node in graph:
                    if isinstance(node, dict):
                        t = node.get("@type")
                        if isinstance(t, list):
                            for x in t:
                                types[str(x)] += 1
                        elif t:
                            types[str(t)] += 1
    return types


def check_structured_data(soups, checks, data):
    CAT = "Structured Data"
    types = extract_jsonld_types(soups)
    data["jsonld_types"] = dict(types)

    make_check(
        checks, CAT, "JSON-LD structured data present",
        ok=sum(types.values()) > 0, max_points=6,
        detail_pass=f"JSON-LD found ({sum(types.values())} blocks: "
                    f"{', '.join(sorted(set(types))[:8])}).",
        detail_fail="No JSON-LD structured data found on sampled pages. AI engines "
                    "use schema to understand who you are and what you offer.",
        fix="Add JSON-LD schema markup site-wide (a schema plugin or custom injection).",
    )

    org = any(t in types for t in
              ("Organization", "LocalBusiness", "LegalService", "ProfessionalService",
               "Attorney", "Store", "Restaurant", "MedicalBusiness", "HomeAndConstructionBusiness"))
    make_check(
        checks, CAT, "Organization / LocalBusiness schema",
        ok=org, max_points=6,
        detail_pass="Business entity schema found - strengthens the brand entity AI engines cite.",
        detail_fail="No Organization/LocalBusiness schema. The site's owner is not "
                    "machine-identifiable as an entity.",
        fix="Add Organization or LocalBusiness JSON-LD with name, logo, address, phone, sameAs links.",
    )

    faq = "FAQPage" in types
    make_check(
        checks, CAT, "FAQPage schema",
        ok=faq, max_points=5, warn=not faq,
        detail_pass="FAQPage schema found - Q&A content is directly extractable by answer engines.",
        detail_fail="No FAQPage schema on sampled pages. FAQ markup maps exactly to "
                    "how users phrase questions to AI.",
        fix="Add FAQ sections with FAQPage JSON-LD to key service/product pages.",
    )

    art = any(t in types for t in ("Article", "BlogPosting", "NewsArticle"))
    make_check(
        checks, CAT, "Article/BlogPosting schema",
        ok=art, max_points=4, warn=not art,
        detail_pass="Article schema found on content pages.",
        detail_fail="No Article/BlogPosting schema detected - posts lose author/date "
                    "signals AI uses to judge credibility and freshness.",
        fix="Emit Article schema with author, datePublished and dateModified on all posts.",
    )

    # canonical + meta description on homepage
    home_soup = soups[0]
    canon = bool(home_soup and home_soup.find("link", rel=lambda v: v and "canonical" in v))
    desc = bool(home_soup and home_soup.find("meta", attrs={"name": "description"}))
    make_check(
        checks, CAT, "Canonical + meta description",
        ok=canon and desc, warn=canon or desc, max_points=4,
        detail_pass="Canonical URL and meta description present.",
        detail_fail=f"Missing: {'canonical tag ' if not canon else ''}"
                    f"{'meta description' if not desc else ''}. These feed the "
                    "snippets retrieval systems evaluate.",
        fix="Add canonical link tags and unique meta descriptions to every page.",
    )


# ----------------------------------------------------------------------------
# Category C: Content Citability (Princeton-style factors)
# ----------------------------------------------------------------------------

def check_content(soups, urls, checks, data):
    CAT = "Content Citability"
    texts, h1s, heading_ok_pages = [], 0, 0
    stats_hits = quotes_hits = list_pages = qa_pages = 0
    outbound_auth = set()
    long_para_pages = 0
    fresh_signals = 0

    content_soups = [s for s in soups if s is not None]
    for soup in content_soups:
        body = BeautifulSoup(str(soup), "html.parser")
        for t in body(["script", "style", "noscript", "nav", "footer", "header"]):
            t.decompose()
        text = body.get_text(" ", strip=True)
        texts.append(text)

        if soup.find("h1"):
            h1s += 1
        hs = [int(h.name[1]) for h in soup.find_all(re.compile("^h[1-4]$"))]
        if hs and hs[0] == 1 and all(b - a <= 1 for a, b in zip(hs, hs[1:]) if b > a):
            heading_ok_pages += 1

        # statistics: percentages, "X out of Y", years with numbers
        if len(re.findall(r"\b\d{1,3}(?:\.\d+)?\s?%|\b\d+\s+(?:out of|in)\s+\d+\b", text)) >= 2:
            stats_hits += 1
        # quotes: blockquote tags or quoted sentences with attribution verbs
        if soup.find("blockquote") or re.search(
                r"[\"\u201c][^\"\u201d]{40,300}[\"\u201d]\s*[-,\u2014]?\s*(said|says|according to|notes|explains)",
                text, re.I):
            quotes_hits += 1
        if soup.find(["ul", "ol", "table"]):
            list_pages += 1
        # Q&A patterns: question headings
        qh = [h for h in soup.find_all(re.compile("^h[2-4]$"))
              if h.get_text(strip=True).endswith("?")
              or re.match(r"(?i)^(how|what|why|when|can|do|does|is|are|should)\b",
                          h.get_text(strip=True))]
        if len(qh) >= 2:
            qa_pages += 1

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(h in href for h in AUTHORITATIVE_HINTS):
                outbound_auth.add(href)

        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        words = [len(p.split()) for p in paras if p]
        if words and (sum(w > 120 for w in words) / len(words)) > 0.4:
            long_para_pages += 1

        page_html = str(soup)
        if (re.search(r"(?i)last\s+updated|updated\s+on", text)
                or re.search(r'property=["\']article:modified_time', page_html)
                or str(CURRENT_YEAR) in text or str(CURRENT_YEAR - 1) in text):
            fresh_signals += 1

    n = max(len(content_soups), 1)
    data["pages_analyzed"] = len(content_soups)

    make_check(checks, CAT, "H1 + logical heading structure",
               ok=h1s == n and heading_ok_pages >= n * 0.6,
               warn=h1s >= n * 0.5, max_points=5,
               detail_pass=f"All {n} sampled pages have an H1 with sane hierarchy.",
               detail_fail=f"Only {h1s}/{n} pages have an H1; hierarchy issues on others. "
                           "AI extraction leans heavily on heading structure.",
               fix="Give every page exactly one H1 and nest H2/H3 logically.")

    make_check(checks, CAT, "Q&A formatted content",
               ok=qa_pages >= max(1, n // 3), warn=qa_pages >= 1, max_points=6,
               detail_pass=f"Question-style headings found on {qa_pages}/{n} pages - "
                           "matches how users prompt AI.",
               detail_fail="Little or no Q&A-structured content. AI queries are questions; "
                           "content shaped as direct answers gets extracted far more often.",
               fix="Add FAQ blocks and question-phrased H2s with a direct 40-60 word answer under each.")

    make_check(checks, CAT, "Statistics & data points",
               ok=stats_hits >= max(1, n // 3), warn=stats_hits >= 1, max_points=5,
               detail_pass=f"Statistics present on {stats_hits}/{n} pages (a top citation driver).",
               detail_fail="Few concrete statistics found. Princeton's GEO research measured "
                           "roughly +30-40% AI visibility for content with citable data points.",
               fix="Add specific numbers, percentages and sourced data to cornerstone pages.")

    make_check(checks, CAT, "Quotations with attribution",
               ok=quotes_hits >= 1, warn=False, max_points=4,
               detail_pass=f"Attributed quotes found on {quotes_hits}/{n} pages.",
               detail_fail="No expert quotes detected - quotations were the single "
                           "strongest factor in the Princeton GEO study (+41%).",
               fix="Add attributed expert quotes (including the client's own expertise) to key pages.")

    make_check(checks, CAT, "Citations to authoritative sources",
               ok=len(outbound_auth) >= 2, warn=len(outbound_auth) == 1, max_points=4,
               detail_pass=f"{len(outbound_auth)} outbound links to authoritative sources found.",
               detail_fail="No outbound links to authoritative sources (.gov, .edu, research, "
                           "major publications). Cited sources make content itself more citable.",
               fix="Reference reputable external sources in cornerstone content.")

    make_check(checks, CAT, "Lists, tables & extractable blocks",
               ok=list_pages >= n * 0.6, warn=list_pages >= 1, max_points=4,
               detail_pass=f"Lists/tables present on {list_pages}/{n} pages.",
               detail_fail="Content is mostly unbroken prose. Answer engines lift lists "
                           "and tables far more readily than paragraphs.",
               fix="Convert comparable info into bulleted lists and tables.")

    make_check(checks, CAT, "Paragraph length (extractability)",
               ok=long_para_pages == 0, warn=long_para_pages <= n // 3, max_points=3,
               detail_pass="Paragraphs are short and extractable.",
               detail_fail=f"{long_para_pages}/{n} pages dominated by very long paragraphs "
                           "(120+ words) - hard for engines to lift cleanly.",
               fix="Break long paragraphs into 2-4 sentence chunks with one idea each.")

    make_check(checks, CAT, "Freshness signals",
               ok=fresh_signals >= n * 0.6, warn=fresh_signals >= 1, max_points=5,
               detail_pass=f"Freshness signals (dates/current year) on {fresh_signals}/{n} pages.",
               detail_fail="Weak freshness signals. AI engines have a strong recency bias; "
                           "undated or stale-looking content loses citations.",
               fix='Show visible "Last updated" dates, keep article:modified_time current, '
                   "and refresh cornerstone pages quarterly.")


# ----------------------------------------------------------------------------
# Category D: Entity & Trust Signals
# ----------------------------------------------------------------------------

def check_entity(base, soups, session, checks, data):
    CAT = "Entity & Trust"
    home = soups[0]

    def link_exists(pattern):
        if home is None:
            return False
        return any(re.search(pattern, a.get("href", ""), re.I)
                   for a in home.find_all("a", href=True))

    about = link_exists(r"about")
    make_check(checks, CAT, "About page discoverable",
               ok=about, max_points=4,
               detail_pass="About page linked from the homepage.",
               detail_fail="No About page link found - weakens the entity profile "
                           "AI engines build for the brand.",
               fix="Publish and link a substantive About page (who, credentials, history).")

    contact = link_exists(r"contact")
    all_text = " ".join(BeautifulSoup(str(s), "html.parser").get_text(" ", strip=True)
                        for s in soups if s is not None)
    phone = bool(re.search(r"(\+?\d[\d\-\s().]{8,}\d)", all_text))
    make_check(checks, CAT, "Contact info (NAP) present",
               ok=contact and phone, warn=contact or phone, max_points=4,
               detail_pass="Contact page and phone number found - consistent NAP "
                           "(name/address/phone) reinforces the business entity.",
               detail_fail="Contact page or visible phone number missing on sampled pages.",
               fix="Show consistent name, address and phone site-wide (footer) and in schema.")

    # author bylines on non-home pages
    bylines = 0
    for s in soups[1:]:
        if s is None:
            continue
        if (s.find(attrs={"rel": "author"})
                or s.find(class_=re.compile("author", re.I))
                or re.search(r"(?i)\bby\s+[A-Z][a-z]+\s+[A-Z][a-z]+", s.get_text(" ", strip=True)[:4000])):
            bylines += 1
    inner = max(len([s for s in soups[1:] if s is not None]), 1)
    make_check(checks, CAT, "Author attribution on content",
               ok=bylines >= inner * 0.4, warn=bylines >= 1, max_points=4,
               detail_pass=f"Author bylines found on {bylines}/{inner} inner pages.",
               detail_fail="Content lacks visible authorship. AI engines weigh "
                           "expertise signals when choosing sources to cite.",
               fix="Add author bylines with credentials, linked to author bio pages with Person schema.")

    # sameAs / social profile links (entity corroboration)
    socials = set()
    if home is not None:
        for a in home.find_all("a", href=True):
            m = re.search(r"(facebook|linkedin|instagram|youtube|x)\.com", a["href"], re.I)
            if m:
                socials.add(m.group(1).lower())
    make_check(checks, CAT, "Cross-platform entity corroboration",
               ok=len(socials) >= 2, warn=len(socials) == 1, max_points=3,
               detail_pass=f"Linked profiles: {', '.join(sorted(socials))} - AI engines "
                           "corroborate entities across platforms.",
               detail_fail="Few or no linked social/business profiles found on the homepage.",
               fix="Link official profiles and add them as sameAs in Organization schema.")


# ----------------------------------------------------------------------------
# Report generation
# ----------------------------------------------------------------------------

GRADE_BANDS = [(90, "A", "#16a34a"), (75, "B", "#65a30d"), (60, "C", "#d97706"),
               (40, "D", "#ea580c"), (0, "F", "#dc2626")]

STATUS_META = {"pass": ("PASS", "#16a34a", "#f0fdf4"),
               "warn": ("NEEDS WORK", "#d97706", "#fffbeb"),
               "fail": ("FAIL", "#dc2626", "#fef2f2")}


def build_html(site, brand, checks, data):
    total = sum(c.points for c in checks)
    maxi = sum(c.max_points for c in checks)
    score = round(100 * total / maxi) if maxi else 0
    grade, color = next((g, c) for t, g, c in GRADE_BANDS if score >= t)

    cats = {}
    for c in checks:
        cats.setdefault(c.category, []).append(c)

    def cat_rows(items):
        rows = ""
        for c in items:
            label, scol, sbg = STATUS_META[c.status]
            fix = (f'<div class="fix"><strong>Recommended fix:</strong> {c.fix}</div>'
                   if c.fix else "")
            rows += f"""
            <div class="check">
              <div class="check-head">
                <span class="badge" style="color:{scol};background:{sbg};border:1px solid {scol}33">{label}</span>
                <span class="check-name">{c.name}</span>
                <span class="pts">{c.points}/{c.max_points}</span>
              </div>
              <div class="detail">{c.detail}</div>
              {fix}
            </div>"""
        return rows

    cat_html = ""
    for cat, items in cats.items():
        earned = sum(c.points for c in items)
        possible = sum(c.max_points for c in items)
        pct = round(100 * earned / possible) if possible else 0
        cat_html += f"""
        <section class="category">
          <div class="cat-head">
            <h2>{cat}</h2>
            <div class="cat-score">{earned}/{possible} pts</div>
          </div>
          <div class="bar"><div class="bar-fill" style="width:{pct}%"></div></div>
          {cat_rows(items)}
        </section>"""

    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    passes = sum(1 for c in checks if c.status == "pass")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>AI Visibility Audit - {site}</title>
<style>
  @page {{ size: A4; margin: 16mm 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; color:#1e293b; margin:0;
         font-size:11.5px; line-height:1.55; }}
  .cover {{ background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%); color:#fff;
            padding:34px 30px; border-radius:14px; margin-bottom:22px; }}
  .cover .brand {{ font-size:11px; letter-spacing:2.5px; text-transform:uppercase;
                   color:#93c5fd; margin-bottom:10px; }}
  .cover h1 {{ margin:0 0 6px; font-size:26px; }}
  .cover .site {{ font-size:14px; color:#cbd5e1; }}
  .scoreband {{ display:flex; align-items:center; gap:24px; margin-top:22px; }}
  .score-circle {{ width:96px; height:96px; border-radius:50%; background:#fff;
                   display:flex; flex-direction:column; align-items:center; justify-content:center; }}
  .score-circle .num {{ font-size:30px; font-weight:800; color:{color}; line-height:1; }}
  .score-circle .of {{ font-size:9px; color:#64748b; }}
  .grade-pill {{ font-size:20px; font-weight:800; background:{color}; padding:8px 20px;
                 border-radius:10px; }}
  .tallies {{ display:flex; gap:14px; font-size:11px; color:#e2e8f0; }}
  .tallies b {{ font-size:16px; display:block; }}
  .summary {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px;
              padding:14px 18px; margin-bottom:20px; }}
  .category {{ margin-bottom:20px; page-break-inside:avoid; }}
  .cat-head {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .cat-head h2 {{ font-size:15px; margin:0 0 6px; color:#0f172a; }}
  .cat-score {{ font-weight:700; color:#475569; }}
  .bar {{ height:7px; background:#e2e8f0; border-radius:4px; margin-bottom:12px; }}
  .bar-fill {{ height:100%; border-radius:4px; background:linear-gradient(90deg,#3b82f6,#1d4ed8); }}
  .check {{ border:1px solid #e2e8f0; border-radius:8px; padding:10px 12px; margin-bottom:8px;
            page-break-inside:avoid; }}
  .check-head {{ display:flex; align-items:center; gap:10px; }}
  .badge {{ font-size:9px; font-weight:800; letter-spacing:.6px; padding:2.5px 8px;
            border-radius:20px; white-space:nowrap; }}
  .check-name {{ font-weight:700; flex:1; }}
  .pts {{ color:#64748b; font-weight:700; }}
  .detail {{ margin-top:5px; color:#334155; }}
  .fix {{ margin-top:6px; background:#eff6ff; border-left:3px solid #3b82f6;
          padding:6px 10px; border-radius:0 6px 6px 0; color:#1e3a8a; }}
  footer {{ margin-top:26px; padding-top:12px; border-top:1px solid #e2e8f0;
            color:#94a3b8; font-size:10px; }}
</style></head>
<body>
  <div class="cover">
    <div class="brand">{brand}</div>
    <h1>AI Visibility Audit</h1>
    <div class="site">{site} &nbsp;&bull;&nbsp; {datetime.now().strftime('%B %d, %Y')}
      &nbsp;&bull;&nbsp; {data.get('pages_analyzed', 0) + 0} pages analyzed</div>
    <div class="scoreband">
      <div class="score-circle"><div class="num">{score}</div><div class="of">/ 100</div></div>
      <div class="grade-pill">Grade {grade}</div>
      <div class="tallies">
        <div><b style="color:#86efac">{passes}</b>passed</div>
        <div><b style="color:#fcd34d">{warns}</b>needs work</div>
        <div><b style="color:#fca5a5">{fails}</b>failed</div>
      </div>
    </div>
  </div>

  <div class="summary">
    <strong>What this report measures.</strong> AI assistants (ChatGPT, Claude, Perplexity,
    Google AI Overviews) now answer a large share of customer questions directly. This audit
    scores whether those engines can <em>access</em>, <em>understand</em>, and <em>cite</em>
    this website - across crawler access, structured data, content citability, and entity trust
    signals. Every failed item below includes the concrete fix we recommend.
  </div>

  {cat_html}

  <footer>Prepared by {brand}. Methodology based on published AI-crawler documentation and
  peer-reviewed GEO research (Princeton/KDD 2024). Scores reflect the sampled pages on the
  audit date.</footer>
</body></html>"""


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run_audit(url, brand, max_pages, out_base):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    root_netloc = parsed.netloc.lower().removeprefix("www.")

    session = requests.Session()
    session.headers["User-Agent"] = UA

    print(f"Auditing {base} ...")
    data = {"url": base, "audited_at": datetime.now().isoformat()}

    home_resp = fetch(base + "/", session)
    if home_resp is None or not home_resp.ok:
        raise RuntimeError("Could not fetch the homepage - site unreachable, "
                           "blocking automated requests, or invalid URL.")
    data["_home_resp"] = home_resp
    home_soup = get_soup(home_resp)

    pages = discover_pages(base + "/", home_soup, root_netloc, max_pages - 1)
    print(f"  homepage + {len(pages)} internal pages selected")
    soups = [home_soup]
    for p in pages:
        soups.append(get_soup(fetch(p, session)))

    checks = []
    check_crawler_access(base, session, checks, data)
    check_structured_data(soups, checks, data)
    check_content(soups, [base] + pages, checks, data)
    check_entity(base, soups, session, checks, data)

    data.pop("_home_resp", None)
    total = sum(c.points for c in checks)
    maxi = sum(c.max_points for c in checks)
    score = round(100 * total / maxi)
    grade = next(g for t, g, _ in GRADE_BANDS if score >= t)
    print(f"  score: {score}/100 (grade {grade})")

    html = build_html(parsed.netloc, brand, checks, data)
    html_path = f"{out_base}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  wrote {html_path}")

    with open(f"{out_base}.json", "w", encoding="utf-8") as f:
        json.dump({"score": score, "grade": grade, "data": data,
                   "checks": [c.as_dict() for c in checks]}, f, indent=2)
    print(f"  wrote {out_base}.json")

    pdf_ok = False
    try:
        from weasyprint import HTML
        HTML(string=html).write_pdf(f"{out_base}.pdf")
        print(f"  wrote {out_base}.pdf")
        pdf_ok = True
    except ImportError:
        print("  (weasyprint not installed - skipped PDF; HTML report is complete)")
    except Exception as e:
        print(f"  (PDF generation failed: {e}; HTML report is complete)")

    return {"score": score, "grade": grade, "pdf": pdf_ok,
            "pages_analyzed": data.get("pages_analyzed", 0)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI Visibility (GEO) Audit")
    ap.add_argument("url", help="Site to audit, e.g. https://example.com")
    ap.add_argument("--brand", default="AI Visibility Audit",
                    help="Your agency/brand name shown on the report")
    ap.add_argument("--max-pages", type=int, default=6,
                    help="Max pages to sample including homepage (default 6)")
    ap.add_argument("--out", default=None, help="Output file base path (no extension)")
    args = ap.parse_args()

    out = args.out or ("geo-audit-" + urlparse(
        args.url if args.url.startswith("http") else "https://" + args.url
    ).netloc.replace(":", "_"))
    run_audit(args.url, args.brand, args.max_pages, out)
