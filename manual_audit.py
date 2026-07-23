#!/usr/bin/env python3
"""
Manual audit catalog: the off-site topics no crawler can verify.

An AI engine forms its picture of a business from the whole web - Google
Business Profile, reviews, directories, mentions - not just the website.
None of that is reliably automatable, so these are human-verified: the
catalog defines WHAT to verify and HOW (step by step); results are recorded
per audit and merged into the reports.

Statuses: pass / warn / fail, or unfilled (not yet verified).
In the CLIENT report, unfilled items render as locked teasers - part of the
full engagement, which is the upsell.
"""

CATEGORY = "Off-Site Presence (verified manually)"

CATALOG = {
    "gbp_complete": {
        "name": "Google Business Profile claimed & complete",
        "max_points": 6, "impact": "critical",
        "why": ("The GBP listing feeds Google's AI surfaces directly and "
                "corroborates the business as a real entity for every other "
                "engine. An unclaimed or half-empty profile suppresses local "
                "AI answers more than almost any on-site factor."),
        "question": "Is the profile claimed, and is every section filled?",
        "how": [
            "Search the business name + city on Google Maps and open the listing.",
            "Check for an 'Own this business?' link - if present, the profile is "
            "UNCLAIMED: automatic fail.",
            "Verify: correct primary category + 2-3 secondary categories, hours, "
            "phone, website link pointing to the audited domain, 10+ photos, "
            "services/products filled, and a business description.",
            "Check the Q&A section - unanswered questions count against completeness.",
            "Pass = claimed and every section above filled. Warn = claimed but "
            "2+ sections empty. Fail = unclaimed or mostly empty.",
        ],
    },
    "reviews_google": {
        "name": "Google review volume, rating & recency",
        "max_points": 5, "impact": "high",
        "why": ("Review count and recency are among the strongest trust inputs "
                "AI engines weigh for 'best X in Y' answers."),
        "question": "Enough reviews, good rating, and recent ones?",
        "how": [
            "On the GBP listing, note the star rating and total review count.",
            "Open the reviews sorted by newest - note the date of the most recent.",
            "Compare against the top 3 competitors for the same search: is this "
            "business in the same league?",
            "Pass = 4.0+, competitive count for the market, and a review within "
            "the last 60 days. Warn = decent rating but stale (nothing in 6 "
            "months) or clearly outnumbered. Fail = under 3.5, under ~10 "
            "reviews, or effectively dormant.",
        ],
    },
    "third_party_reviews": {
        "name": "Industry review platforms",
        "max_points": 4, "impact": "high",
        "why": ("AI engines cite third-party platforms heavily - for lawyers "
                "Avvo/Justia, for trades Yelp/Angi, for B2B Clutch/G2. A "
                "business absent from its industry's platforms is invisible in "
                "the sources AI actually quotes."),
        "question": "Present and reviewed on the platforms for this industry?",
        "how": [
            "Identify the 2-3 platforms that matter for this vertical (legal: "
            "Avvo, Justia, state bar directory; trades: Yelp, Angi; medical: "
            "Healthgrades; B2B: Clutch, G2).",
            "Search the business on each. Note: profile exists? claimed? has "
            "reviews? rating?",
            "Pass = claimed profiles with reviews on the key platforms. Warn = "
            "listed but unclaimed or reviewless. Fail = absent from all.",
        ],
    },
    "nap_consistency": {
        "name": "NAP consistency across directories",
        "max_points": 4, "impact": "medium",
        "why": ("Machines match entities by exact string comparison. 'Suite "
                "200' vs 'Ste. 200' across listings splits the business into "
                "weaker duplicate entities."),
        "question": "Identical name, address, phone everywhere?",
        "how": [
            "Copy the exact NAP from the website footer.",
            "Check it character-for-character against: GBP, the top 2 industry "
            "directories, Facebook, and Yelp.",
            "Log every mismatch (old address, different phone format, name "
            "variants) in the notes - that list becomes the fix ticket.",
            "Pass = identical everywhere. Warn = formatting drift only. "
            "Fail = conflicting addresses/phones or duplicate listings.",
        ],
    },
    "social_active": {
        "name": "Social profiles exist & are active",
        "max_points": 3, "impact": "medium",
        "why": ("Active profiles corroborate the entity and create citable "
                "surface. Dead profiles (last post 2022) can read worse than "
                "none - they suggest a defunct business."),
        "question": "Do linked profiles exist, and posted in the last 90 days?",
        "how": [
            "Open every social profile linked from the site (and search for "
            "unlinked ones on Facebook/LinkedIn/YouTube).",
            "Note the date of the most recent post on each.",
            "Pass = 2+ profiles with activity in the last 90 days. Warn = "
            "profiles exist but stale. Fail = none exist, or links are broken.",
        ],
    },
    "brand_mentions": {
        "name": "Brand mentions & citations on the wider web",
        "max_points": 4, "impact": "medium",
        "why": ("Most AI citations come from third-party sources, not the "
                "brand's own site. News mentions, local press, podcasts and "
                "association pages are what engines quote when naming a "
                "business."),
        "question": "Does the wider web talk about this business?",
        "how": [
            'Search: "Business Name" -site:theirdomain.com on Google.',
            "Also search the name on Google News and in Perplexity ('what do "
            "you know about <business> in <city>').",
            "Note the quality: local news, chamber of commerce, association "
            "memberships, sponsorships, podcast appearances.",
            "Pass = multiple genuine third-party mentions incl. at least one "
            "news/association source. Warn = only directory listings. Fail = "
            "the brand basically doesn't exist off its own site.",
        ],
    },
    "knowledge_presence": {
        "name": "Knowledge panel / structured web presence",
        "max_points": 3, "impact": "low",
        "why": ("A Google knowledge panel, Wikidata entry or Crunchbase page "
                "means the business exists as an ENTITY in the graphs AI "
                "engines consult, not just as a website."),
        "question": "Does the business exist in knowledge graphs?",
        "how": [
            "Search the exact business name on Google - does a knowledge panel "
            "appear on the right?",
            "Check wikidata.org and (for companies) Crunchbase for an entry.",
            "Pass = knowledge panel or a graph entry exists. Warn = panel "
            "shows but sparse/wrong. Fail = no structured presence. (Fail is "
            "NORMAL for small local businesses - it's an opportunity note, "
            "not an emergency.)",
        ],
    },
    "competitor_gap": {
        "name": "Competitor gap review",
        "max_points": 4, "impact": "high",
        "why": ("The businesses AI engines DO recommend define the bar. "
                "Knowing what their pages and profiles have that this "
                "business lacks converts directly into the work plan."),
        "question": "Who wins the AI answers now, and why?",
        "how": [
            "Take the 2-3 competitors your AI Tests rounds recorded as "
            "recommended (the notes field - this is why we log them).",
            "Open their sites and GBP listings next to the client's.",
            "List concretely what they have that the client lacks: review "
            "count, FAQ depth, pricing transparency, local content, "
            "directory presence.",
            "Pass = client matches or beats the winners on most factors. "
            "Warn = clear but closable gaps. Fail = outclassed across the "
            "board. Put the gap list in the notes - it IS the proposal.",
        ],
    },
}

STATUS_POINTS = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


def merged(results_by_key):
    """Merge catalog with recorded results (dict key -> row or None).
    Returns list of dicts ready for rendering, catalog order preserved."""
    out = []
    for key, spec in CATALOG.items():
        r = results_by_key.get(key)
        status = r["status"] if r else None
        pts = round(spec["max_points"] * STATUS_POINTS[status]) if status else 0
        out.append({
            "key": key, "name": spec["name"], "impact": spec["impact"],
            "why": spec["why"], "question": spec["question"],
            "how": spec["how"], "max_points": spec["max_points"],
            "status": status, "points": pts,
            "notes": (r["notes"] if r else "") or "",
        })
    return out


def extended_score(items):
    """(earned, possible) over FILLED items only; (0, 0) when none filled."""
    filled = [i for i in items if i["status"]]
    if not filled:
        return 0, 0
    return (sum(i["points"] for i in filled),
            sum(i["max_points"] for i in filled))
