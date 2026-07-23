#!/usr/bin/env python3
"""
Deep Scan for the GEO audit engine.

Two additions on top of the deterministic core:

1. Originality analysis (DETERMINISTIC, always on when 3+ pages sampled).
   Detects template/programmatic content: how much of each page is repeated
   verbatim on sibling pages, and whether pages become near-identical once
   place names are masked (the classic PSEO city-swap pattern). AI engines
   deprioritise duplicated template content, so a high template ratio directly
   suppresses citation odds.

2. LLM-as-judge (OPT-IN "deep scan", costs one LLM API call per audit).
   Asks the configured LLM to act as an AI answer engine looking at the
   homepage: infer the query the page targets, judge whether it would cite
   the page, and check which basic customer questions the page can answer.
   Non-deterministic by nature - scores can vary a little between runs -
   which is why it is layered on top of the deterministic core rather than
   replacing it.
"""

import json
import re

import requests

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-haiku-4-5",
}


# ----------------------------------------------------------------------------
# 1. Originality (deterministic)
# ----------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_CAP_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b")


def _page_text(soup):
    body = soup.body or soup
    for tag in body.find_all(["script", "style", "noscript"]):
        tag.decompose()
    return body.get_text(" ", strip=True)


def _shingles(text, k=8):
    words = _WORD_RE.findall(text.lower())
    return {" ".join(words[i:i + k]) for i in range(max(len(words) - k + 1, 0))}


def _masked(text):
    """Replace capitalised word runs (place/brand names) with a placeholder so
    'Plumbers in Dallas' and 'Plumbers in Austin' compare as identical."""
    return _CAP_RE.sub("~", text)


def originality_metrics(soups):
    """Cross-page duplication metrics for the sampled pages.

    Returns None when fewer than 3 usable pages (not enough signal), else:
      template_ratio  - avg fraction of a page's shingles found on other pages
      cityswap_pairs  - page pairs that are <80% similar raw but >=90% similar
                        once capitalised names are masked (city-swap pattern)
      pages_used
    """
    texts = []
    for s in soups:
        if s is None:
            continue
        t = _page_text(s)
        if len(t) > 400:            # skip stubs; they'd skew the ratio
            texts.append(t)
    if len(texts) < 3:
        return None

    texts = texts[:8]               # cap the O(n^2) comparisons
    shingle_sets = [_shingles(t) for t in texts]
    ratios = []
    for i, sh in enumerate(shingle_sets):
        if not sh:
            continue
        others = set().union(*(s for j, s in enumerate(shingle_sets) if j != i))
        ratios.append(len(sh & others) / len(sh))
    template_ratio = sum(ratios) / len(ratios) if ratios else 0.0

    def _sim(a, b):
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    masked_sets = [_shingles(_masked(t)) for t in texts]
    cityswap = 0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            raw = _sim(shingle_sets[i], shingle_sets[j])
            masked = _sim(masked_sets[i], masked_sets[j])
            if raw < 0.80 and masked >= 0.90:
                cityswap += 1
    return {"template_ratio": template_ratio, "cityswap_pairs": cityswap,
            "pages_used": len(texts)}


# ----------------------------------------------------------------------------
# 2. LLM-as-judge (opt-in)
# ----------------------------------------------------------------------------

JUDGE_PROMPT = """You are the retrieval component of an AI answer engine
(like ChatGPT search or Perplexity) evaluating one web page.

Page URL: {url}
Page content (extracted text, truncated):
---
{text}
---

Respond with ONLY valid JSON, no markdown fences, in exactly this shape:
{{
  "target_query": "the most likely user question this page is trying to answer",
  "citation_score": 0-10,
  "citation_reason": "2 sentences: would you cite this page for that query, and why/why not",
  "coverage": {{
    "what_they_do": true/false,
    "where_they_operate": true/false,
    "cost_or_pricing": true/false,
    "process_or_timeline": true/false,
    "who_its_for": true/false,
    "common_questions": true/false
  }}
}}

Judge citation_score strictly: 8-10 only for pages you would confidently cite
verbatim; 4-7 for usable but weak; 0-3 for pages with nothing quotable.
Base every judgment ONLY on the text above. Do not invent content."""


def _extract_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in LLM response")
    return json.loads(m.group(0))


def llm_judge(llm_cfg, url, page_text):
    """One LLM call: infer target query, citation verdict, answer coverage.
    llm_cfg: {"provider","api_key","model"(optional)}. Raises on failure."""
    provider = llm_cfg["provider"].lower()
    model = llm_cfg.get("model") or DEFAULT_MODELS.get(provider)
    if provider not in DEFAULT_MODELS:
        raise RuntimeError(f"provider must be one of {list(DEFAULT_MODELS)}")
    prompt = JUDGE_PROMPT.format(url=url, text=page_text[:6000])

    if provider == "openai":
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
                          json={"model": model, "max_tokens": 500,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=60)
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"]
    elif provider == "gemini":
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": llm_cfg["api_key"]},
            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
        r.raise_for_status()
        out = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    else:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": llm_cfg["api_key"],
                                   "anthropic-version": "2023-06-01"},
                          json={"model": model, "max_tokens": 500,
                                "messages": [{"role": "user", "content": prompt}]},
                          timeout=60)
        r.raise_for_status()
        out = r.json()["content"][0]["text"]

    d = _extract_json(out)
    score = max(0, min(int(d.get("citation_score", 0)), 10))
    cov = d.get("coverage") or {}
    covered = [k for k, v in cov.items() if v]
    return {
        "target_query": str(d.get("target_query", ""))[:200],
        "citation_score": score,
        "citation_reason": str(d.get("citation_reason", ""))[:500],
        "covered": covered,
        "missing": [k for k in ("what_they_do", "where_they_operate",
                                "cost_or_pricing", "process_or_timeline",
                                "who_its_for", "common_questions")
                    if k not in covered],
    }


def label(key):
    return key.replace("_", " ")
