#!/usr/bin/env python3
"""
Hand-written remediation guides, one per audit check.

Deterministic (no LLM, no API cost, no hallucinated menu paths). Each entry is
a list of steps; a step is either:
    ("p",    "paragraph text")            - prose, may contain <b>/<code>
    ("ol",   ["step one", "step two"])    - numbered instructions
    ("ul",   ["point", "point"])          - bullets
    ("code", "label", "the code block")   - copy-paste block
    ("note", "text")                      - caution / gotcha callout

Use {domain} in any string; it is replaced with the audited site's domain.
"""

GUIDES = {

# ---------------------------------------------------------------------------
# AI Crawler Access
# ---------------------------------------------------------------------------
"AI crawlers allowed in robots.txt": [
    ("p", "Your robots.txt is currently telling one or more AI engines not to "
          "read the site. Until this is changed, those engines cannot cite you "
          "under any circumstances - no amount of content or schema work will "
          "help. This is the first thing to fix."),
    ("ol", [
        "Open your robots.txt editor: <b>Yoast SEO &rarr; Tools &rarr; File Editor</b>, "
        "or <b>Rank Math &rarr; General Settings &rarr; Edit robots.txt</b>. If you "
        "have neither, edit <code>public_html/robots.txt</code> in your host's file "
        "manager (cPanel, CyberPanel, Plesk).",
        "Find and delete any block that disallows an AI bot - anything of the form "
        "<code>User-agent: GPTBot</code> followed by <code>Disallow: /</code>.",
        "Add the directives below, then save.",
    ]),
    ("code", "robots.txt",
     "# AI answer engines - allow\n"
     "User-agent: GPTBot\n"
     "Allow: /\n\n"
     "User-agent: OAI-SearchBot\n"
     "Allow: /\n\n"
     "User-agent: ChatGPT-User\n"
     "Allow: /\n\n"
     "User-agent: ClaudeBot\n"
     "Allow: /\n\n"
     "User-agent: Claude-User\n"
     "Allow: /\n\n"
     "User-agent: PerplexityBot\n"
     "Allow: /\n\n"
     "User-agent: Perplexity-User\n"
     "Allow: /\n\n"
     "User-agent: Google-Extended\n"
     "Allow: /\n\n"
     "User-agent: *\n"
     "Allow: /\n\n"
     "Sitemap: https://{domain}/sitemap_index.xml"),
    ("p", "<b>Verify:</b> visit <code>https://{domain}/robots.txt</code> in a browser "
          "and confirm the new rules are live."),
    ("note", "A business decision to make first: allowing <code>GPTBot</code> and "
             "<code>Google-Extended</code> also permits your content to be used for "
             "AI model <i>training</i>, not just for answering questions about you. "
             "Most businesses want the visibility and accept this. If you want "
             "retrieval without training, allow <code>OAI-SearchBot</code> and "
             "<code>ChatGPT-User</code> but disallow <code>GPTBot</code> - accepting "
             "that this reduces your reach."),
],

"robots.txt reachable": [
    ("p", "No robots.txt was found. Crawlers fall back to defaults, so you are not "
          "necessarily blocked - but you lose explicit control over AI crawler "
          "access and you lose the sitemap hint that helps engines discover pages."),
    ("ol", [
        "Create a file named <code>robots.txt</code> in your site root "
        "(<code>public_html/</code>), or use Yoast/Rank Math's robots.txt editor "
        "which creates it for you.",
        "Paste the contents below and save.",
    ]),
    ("code", "robots.txt",
     "User-agent: GPTBot\nAllow: /\n\n"
     "User-agent: OAI-SearchBot\nAllow: /\n\n"
     "User-agent: ClaudeBot\nAllow: /\n\n"
     "User-agent: PerplexityBot\nAllow: /\n\n"
     "User-agent: Google-Extended\nAllow: /\n\n"
     "User-agent: *\nAllow: /\n\n"
     "Sitemap: https://{domain}/sitemap_index.xml"),
    ("p", "<b>Verify:</b> <code>https://{domain}/robots.txt</code> should load in a browser."),
],

"CDN bot-blocking risk (Cloudflare)": [
    ("p", "This site is behind Cloudflare. Cloudflare's newer defaults can block AI "
          "crawlers <b>at the network level</b> - meaning the request never reaches "
          "your site at all, even when robots.txt explicitly allows the bot. This is "
          "invisible from the website itself, and it is one of the most common "
          "reasons a well-optimized site gets no AI citations."),
    ("ol", [
        "Log in to the <b>Cloudflare dashboard</b> and select this domain.",
        "Go to <b>Security &rarr; Bots</b>. Look for <b>AI Scrapers and Crawlers</b> "
        "(sometimes shown as 'Block AI bots'). If it is ON, turn it OFF.",
        "Go to <b>Security &rarr; WAF &rarr; Custom rules</b> and check for any rule "
        "that blocks requests by user-agent. Disable or amend any rule matching AI "
        "bot names.",
        "Check <b>Security &rarr; Settings</b> for 'Bot Fight Mode' - this can also "
        "challenge AI crawlers. Turn it off, or move to Super Bot Fight Mode where "
        "you can allow specific bots.",
    ]),
    ("p", "<b>Verify:</b> run these three commands from any terminal. Each must return "
          "<code>HTTP/2 200</code>. A <code>403</code> means the bot is still blocked."),
    ("code", "terminal - verification",
     'curl -A "GPTBot" -I https://{domain}/\n'
     'curl -A "ClaudeBot" -I https://{domain}/\n'
     'curl -A "PerplexityBot" -I https://{domain}/'),
    ("note", "Menu names in Cloudflare change from time to time. If you cannot find "
             "the AI bot setting, search the dashboard for 'AI' - the control exists "
             "on all plan tiers including Free."),
],

"CDN bot-blocking risk": [
    ("p", "No CDN-level AI bot blocking was detected. Nothing to do here - but if you "
          "later put the site behind Cloudflare or a similar CDN, re-check this: many "
          "CDNs now block AI crawlers by default."),
],

"Content readable without JavaScript": [
    ("p", "Most AI crawlers <b>do not execute JavaScript</b>. They read the raw HTML "
          "your server returns. If your content is injected by scripts after the page "
          "loads, AI engines see an effectively empty page - regardless of how good "
          "the content looks in a browser."),
    ("ol", [
        "Confirm the problem: run the command below and search the output for a "
        "sentence you can see on the live page. If it is not there, AI cannot see it "
        "either.",
    ]),
    ("code", "terminal - what AI actually sees",
     "curl -s https://{domain}/ | grep -i \"a sentence from your homepage\""),
    ("p", "Common causes on WordPress and how to fix each:"),
    ("ul", [
        "<b>Aggressive lazy-loading of text</b> - some optimization plugins (WP Rocket, "
        "LiteSpeed, Perfmatters) defer or lazy-render content blocks. Disable "
        "'delay JavaScript execution' and any 'lazy render' / 'content visibility' "
        "option, then re-test.",
        "<b>Tabs and accordions</b> - Elementor tab and toggle widgets usually keep "
        "content in the HTML, but some third-party widgets inject it on click. If so, "
        "move the important content into a plain text block.",
        "<b>A React/Vue/headless front end</b> - this needs server-side rendering "
        "(SSR) or pre-rendering. This is a developer task, not a settings change.",
        "<b>Content loaded by AJAX</b> - e.g. 'load more' reviews or services. Render "
        "at least the first batch server-side.",
    ]),
    ("p", "<b>Verify:</b> re-run the curl command. Your key headings and paragraphs "
          "should appear in the raw HTML."),
],

"llms.txt present": [
    ("p", "<code>llms.txt</code> is an emerging convention: a plain-text file at your "
          "site root that gives AI systems a clean, curated map of your most important "
          "pages. It is not yet universally supported, but it costs ten minutes and "
          "signals that the site is AI-ready."),
    ("ol", [
        "Create a file named <code>llms.txt</code> in your site root "
        "(<code>public_html/llms.txt</code>) via your host's file manager.",
        "Use the structure below - a title, a one-line description, then your key "
        "pages grouped under headings, each with a short description.",
        "Save, then confirm it loads at <code>https://{domain}/llms.txt</code>.",
    ]),
    ("code", "llms.txt",
     "# Your Business Name\n\n"
     "> One sentence describing what the business does, where it operates,\n"
     "> and who it serves.\n\n"
     "## Services\n"
     "- [Service One](https://{domain}/service-one): What it is, who it's for, price range\n"
     "- [Service Two](https://{domain}/service-two): What it is, who it's for, price range\n\n"
     "## Information\n"
     "- [FAQ](https://{domain}/faq): Common questions answered\n"
     "- [About](https://{domain}/about): History, credentials, team\n"
     "- [Contact](https://{domain}/contact): Location, hours, how to reach us"),
    ("note", "Keep it short and honest. This is a map, not a marketing page. List only "
             "pages you actually want cited."),
],

"XML sitemap present": [
    ("p", "AI answer engines discover pages through the same indexes that power "
          "traditional search. Without a sitemap, pages may never be found - and a "
          "page that is never crawled can never be cited."),
    ("ol", [
        "If you use <b>Yoast SEO</b>: go to <b>Yoast &rarr; Settings &rarr; Site "
        "features</b> and ensure <b>XML sitemaps</b> is ON.",
        "If you use <b>Rank Math</b>: <b>Rank Math &rarr; Sitemap Settings</b> and "
        "ensure the sitemap module is enabled.",
        "If you use neither, install one of them - it takes two minutes and both are free.",
        "Confirm the sitemap loads at <code>https://{domain}/sitemap_index.xml</code> "
        "(Yoast/Rank Math) or <code>https://{domain}/sitemap.xml</code>.",
        "Add the sitemap line to robots.txt so crawlers find it automatically.",
    ]),
    ("code", "add to robots.txt",
     "Sitemap: https://{domain}/sitemap_index.xml"),
    ("p", "<b>Also do this:</b> submit the sitemap in <b>Google Search Console</b> "
          "(Indexing &rarr; Sitemaps). Google's AI surfaces draw heavily on Google's "
          "own index."),
],

# ---------------------------------------------------------------------------
# Structured Data
# ---------------------------------------------------------------------------
"JSON-LD structured data present": [
    ("p", "Schema markup (JSON-LD) is how a machine understands what your business is, "
          "what it sells, and where it operates. Without it, an AI engine has to guess "
          "from prose - and it will usually prefer a competitor whose markup is explicit."),
    ("ol", [
        "Add the Organization/LocalBusiness block from the next section - that is the "
        "foundation and should exist on every page.",
        "Add FAQPage schema to your service pages (see the FAQPage section).",
        "Ensure your SEO plugin is emitting Article schema on blog posts - Yoast and "
        "Rank Math both do this automatically when configured.",
    ]),
    ("p", "The fastest route on WordPress: <b>Rank Math &rarr; Schema</b> or "
          "<b>Yoast &rarr; Settings &rarr; Site representation</b> handles the basics "
          "with no code. For anything custom, paste JSON-LD into "
          "<b>Elementor &rarr; Site Settings &rarr; Custom Code</b> with location "
          "<code>&lt;head&gt;</code>, or use the free "
          "<i>Insert Headers and Footers</i> plugin."),
],

"Organization / LocalBusiness schema": [
    ("p", "This is the single most important piece of schema. It tells AI engines that "
          "a specific, real business exists at a specific place, with a name, phone, "
          "address, and verifiable presence elsewhere on the web. It is the anchor for "
          "every AI answer that might otherwise name a competitor."),
    ("ol", [
        "Open <b>Elementor &rarr; Site Settings &rarr; Custom Code &rarr; Add Code</b>, "
        "set location to <code>&lt;head&gt;</code>. (Or use the "
        "<i>Insert Headers and Footers</i> plugin, header section.)",
        "Paste the block below and replace every placeholder with the real details.",
        "Set <code>@type</code> to match the business - see the note underneath.",
        "Publish, then validate (see Verify below).",
    ]),
    ("code", "JSON-LD - paste in &lt;head&gt;",
     '<script type="application/ld+json">\n'
     '{\n'
     '  "@context": "https://schema.org",\n'
     '  "@type": "LocalBusiness",\n'
     '  "name": "Your Business Name",\n'
     '  "description": "One clear sentence about what you do and where.",\n'
     '  "url": "https://{domain}",\n'
     '  "logo": "https://{domain}/wp-content/uploads/logo.png",\n'
     '  "image": "https://{domain}/wp-content/uploads/premises.jpg",\n'
     '  "telephone": "+1-555-000-0000",\n'
     '  "priceRange": "$$",\n'
     '  "address": {\n'
     '    "@type": "PostalAddress",\n'
     '    "streetAddress": "123 Example Street",\n'
     '    "addressLocality": "City",\n'
     '    "addressRegion": "State",\n'
     '    "postalCode": "00000",\n'
     '    "addressCountry": "US"\n'
     '  },\n'
     '  "geo": {\n'
     '    "@type": "GeoCoordinates",\n'
     '    "latitude": 00.0000,\n'
     '    "longitude": 00.0000\n'
     '  },\n'
     '  "areaServed": [\n'
     '    { "@type": "City", "name": "Primary City" },\n'
     '    { "@type": "City", "name": "Secondary City" }\n'
     '  ],\n'
     '  "openingHoursSpecification": [{\n'
     '    "@type": "OpeningHoursSpecification",\n'
     '    "dayOfWeek": ["Monday","Tuesday","Wednesday","Thursday","Friday"],\n'
     '    "opens": "09:00",\n'
     '    "closes": "17:00"\n'
     '  }],\n'
     '  "sameAs": [\n'
     '    "https://www.google.com/maps/place/YOUR-LISTING",\n'
     '    "https://www.facebook.com/yourpage",\n'
     '    "https://www.linkedin.com/company/yourpage"\n'
     '  ]\n'
     '}\n'
     '</script>'),
    ("p", "<b>Set <code>@type</code> correctly.</b> This determines how the engine "
          "categorises the business:"),
    ("ul", [
        "Law firm &rarr; <code>LegalService</code> or <code>Attorney</code>",
        "Medical / dental &rarr; <code>MedicalBusiness</code> or <code>Dentist</code>",
        "Restaurant &rarr; <code>Restaurant</code>",
        "Contractor, roofing, duct cleaning, trades &rarr; <code>HomeAndConstructionBusiness</code>",
        "Accountant &rarr; <code>AccountingService</code>",
        "Online store &rarr; <code>OnlineStore</code>",
        "Anything else local &rarr; <code>LocalBusiness</code>",
    ]),
    ("p", "<b>Verify:</b> paste <code>https://{domain}</code> into "
          "<b>search.google.com/test/rich-results</b> and confirm the business entity "
          "is detected with no errors."),
    ("note", "The <code>sameAs</code> field is quietly one of the most valuable. It is "
             "how AI corroborates that you are a real entity that exists in more than "
             "one place. Include the Google Business Profile listing, social profiles, "
             "and any industry directory you appear in."),
],

"FAQPage schema": [
    ("p", "FAQ markup maps <b>exactly</b> onto how people use AI. Someone types a "
          "question; the engine looks for a source that contains that question with a "
          "clean, direct answer underneath. This is the highest-return schema type for "
          "AI visibility."),
    ("ol", [
        "On each key service page, add a real FAQ section with 3-6 questions - written "
        "in the words your customers actually use when they phone you.",
        "Answer each in 40-60 words, directly, with a concrete fact or number in the "
        "first sentence.",
        "Mirror those exact questions and answers into the JSON-LD below and paste it "
        "into that page's custom code (Elementor: page settings &rarr; Custom Code).",
    ]),
    ("code", "JSON-LD - FAQPage (one per page)",
     '<script type="application/ld+json">\n'
     '{\n'
     '  "@context": "https://schema.org",\n'
     '  "@type": "FAQPage",\n'
     '  "mainEntity": [\n'
     '    {\n'
     '      "@type": "Question",\n'
     '      "name": "How much does [your service] cost?",\n'
     '      "acceptedAnswer": {\n'
     '        "@type": "Answer",\n'
     '        "text": "State the real range and your actual price. Example: [Service] '
     'typically costs $X to $Y depending on [factor]. We charge a flat $Z for [common case]."\n'
     '      }\n'
     '    },\n'
     '    {\n'
     '      "@type": "Question",\n'
     '      "name": "How long does [your service] take?",\n'
     '      "acceptedAnswer": {\n'
     '        "@type": "Answer",\n'
     '        "text": "Give the real timeframe and a supporting number. Example: Most '
     'jobs take 2 to 4 hours. In 2025, 78% of ours were completed in under 3 hours."\n'
     '      }\n'
     '    }\n'
     '  ]\n'
     '}\n'
     '</script>'),
    ("note", "<b>Critical:</b> the answers in the schema must match text that is "
             "actually visible on the page. Putting answers in the markup that do not "
             "appear on the page is cloaking, and it can get the page penalised. Write "
             "the visible FAQ first, then mirror it."),
    ("p", "<b>Verify:</b> the Rich Results Test should report FAQ detected with no errors."),
],

"Article/BlogPosting schema": [
    ("p", "Article schema supplies the author and date signals AI engines use to judge "
          "whether a piece of content is credible and current."),
    ("ol", [
        "In <b>Yoast</b>: ensure each post has an author assigned and that "
        "<b>Yoast &rarr; Settings &rarr; Content types &rarr; Posts</b> has schema set "
        "to <i>Article</i>.",
        "In <b>Rank Math</b>: <b>Rank Math &rarr; Titles &amp; Meta &rarr; Posts</b>, "
        "set Schema Type to <i>Article</i>.",
        "Confirm every post shows a real author (not 'admin') and a visible date.",
        "Whenever you update a post, make sure the <b>modified date</b> updates - this "
        "feeds the freshness signal AI engines weight heavily.",
    ]),
    ("p", "<b>Verify:</b> Rich Results Test on any blog post should show Article with "
          "<code>author</code>, <code>datePublished</code>, and <code>dateModified</code>."),
],

"Canonical + meta description": [
    ("p", "Canonical tags prevent duplicate-content confusion; meta descriptions feed "
          "the snippets that retrieval systems evaluate. Both are basic hygiene that "
          "your SEO plugin handles once configured."),
    ("ol", [
        "Yoast/Rank Math both emit canonical tags automatically - confirm the plugin is "
        "active and no theme setting is overriding it.",
        "Write a unique meta description for every important page: 120-155 characters, "
        "stating plainly what the page offers. Do not leave them auto-generated on "
        "service pages.",
        "Check for accidental duplicates - two pages with identical descriptions is a "
        "signal that one of them is thin.",
    ]),
],

# ---------------------------------------------------------------------------
# Content Citability
# ---------------------------------------------------------------------------
"Q&A formatted content": [
    ("p", "People do not type keywords into AI - they ask questions. Content structured "
          "as a question followed by a direct answer is what gets extracted and quoted. "
          "Marketing prose almost never is."),
    ("p", "<b>The rewrite pattern.</b> On every cornerstone page, convert statement "
          "headings into question headings, and put a direct answer immediately below:"),
    ("ul", [
        "<i>Before:</i> \"Our Services\" &rarr; <i>After:</i> \"What services do we offer "
        "in [City]?\"",
        "<i>Before:</i> \"Pricing\" &rarr; <i>After:</i> \"How much does [service] cost "
        "in [City]?\"",
        "<i>Before:</i> \"Why Choose Us\" &rarr; <i>After:</i> \"How do I choose a "
        "[service provider] in [City]?\"",
    ]),
    ("ol", [
        "Ask the business owner: <b>what do customers actually ask on the phone?</b> "
        "Those exact phrasings become your H2 headings. This is the single highest-value "
        "input and it takes one conversation.",
        "Under each question heading, answer in the <b>first 40-60 words</b>. Do not "
        "build up to it - AI lifts the top of the section.",
        "Include a concrete number or fact in that first answer wherever possible.",
        "Add 3-6 such question sections per key page, then mirror them into FAQPage "
        "schema (see that section).",
    ]),
    ("note", "Long questions get broken by AI into smaller sub-queries and searched "
             "separately. \"Best duct cleaner in Melbourne for a house with pets\" may "
             "fan out into \"duct cleaning Melbourne\", \"duct cleaning pet dander\", "
             "and \"duct cleaning cost\". Make sure you have content answering each "
             "fragment, not just the whole question."),
],

"Statistics & data points": [
    ("p", "Concrete figures are one of the strongest measured drivers of AI citation. "
          "An engine writing an answer needs something specific to quote; \"we deliver "
          "exceptional service\" gives it nothing, while \"78% of jobs completed in "
          "under 3 hours\" gives it a sentence it can use."),
    ("p", "<b>Convert every vague claim into a specific one.</b> Work through your key "
          "pages and replace adjectives with numbers:"),
    ("ul", [
        "\"Fast service\" &rarr; \"Same-week booking; 78% of jobs completed within 3 hours\"",
        "\"Experienced team\" &rarr; \"20 years in business, 12,000+ jobs completed since 2005\"",
        "\"Affordable\" &rarr; \"Flat $299 for homes up to 10 vents, no call-out fee\"",
        "\"Highly rated\" &rarr; \"4.8 stars across 340 Google reviews\"",
    ]),
    ("ol", [
        "Get the real numbers from the business owner - job counts, years trading, "
        "warranty length, response times, review counts, price ranges.",
        "Put at least 2-3 verifiable figures on every cornerstone page.",
        "Add a pricing table where prices vary - tables are lifted into AI answers far "
        "more readily than prose.",
    ]),
    ("note", "Only use real numbers. Inventing statistics to win citations is both "
             "dishonest and legally risky in regulated industries such as law, medicine, "
             "and finance."),
],

"Quotations with attribution": [
    ("p", "In published GEO research, attributed quotations were the single strongest "
          "content factor for increasing AI citation rates. A quote gives the engine a "
          "self-contained, credible, quotable unit."),
    ("ol", [
        "Add at least one attributed quote to each cornerstone page.",
        "Attribute it to a real, named person - the owner, a senior technician, a named "
        "customer, or a recognised industry authority.",
        "Mark it up as a real <code>&lt;blockquote&gt;</code> so it is structurally "
        "identifiable, not just italic text.",
    ]),
    ("p", "Sources of quotes you already have:"),
    ("ul", [
        "<b>Customer reviews</b> - lift a specific line from a Google review, with the "
        "reviewer's first name and month.",
        "<b>The owner's own expertise</b> - interview them for 15 minutes and quote them "
        "directly on a common customer question.",
        "<b>Industry bodies or regulators</b> - quote published guidance, with a link.",
    ]),
    ("code", "HTML - a properly marked-up quote",
     '<blockquote>\n'
     '  "The difference in air quality was noticeable within a day - my daughter\'s\n'
     '   asthma symptoms dropped straight away."\n'
     '  <cite>Sarah M., Brunswick - March 2026 Google review</cite>\n'
     '</blockquote>'),
],

"Citations to authoritative sources": [
    ("p", "This one is counterintuitive: linking <i>out</i> to credible sources makes "
          "your own page more likely to be cited. Engines treat sourced content as more "
          "trustworthy than unsourced assertion."),
    ("ol", [
        "On each cornerstone page, add 1-3 outbound links to genuinely authoritative "
        "sources that support a claim you are making.",
        "Prefer government (<code>.gov</code>), education (<code>.edu</code>), "
        "regulators, industry associations, and published research.",
        "Link in context, on a real claim - not a 'Resources' dump at the bottom of the page.",
    ]),
    ("p", "Examples by sector:"),
    ("ul", [
        "<b>Trades / home services</b> - the EPA, national standards bodies, "
        "manufacturer guidance",
        "<b>Legal</b> - the state bar, statutes, court sites",
        "<b>Medical / dental</b> - the relevant health department, NIH, professional colleges",
        "<b>Finance</b> - the regulator, tax authority publications",
    ]),
    ("note", "Do not link to competitors, and do not add links purely for the sake of "
             "it. One well-placed link on a real claim beats five decorative ones."),
],

"Lists, tables & extractable blocks": [
    ("p", "AI engines lift lists and tables far more readily than paragraphs, because "
          "the structure tells them where one item ends and the next begins."),
    ("ol", [
        "Find any place on the site where you are comparing, enumerating, or sequencing "
        "in prose, and convert it to a list or table.",
        "Pricing by tier or size &rarr; a table.",
        "Service areas &rarr; a bulleted list.",
        "Your process &rarr; a numbered list.",
        "What is / is not included &rarr; a two-column table.",
    ]),
    ("p", "Use real HTML <code>&lt;table&gt;</code>, <code>&lt;ul&gt;</code>, and "
          "<code>&lt;ol&gt;</code> elements. In Elementor, use the Text Editor widget's "
          "list buttons or a proper Table widget - not manually typed dashes or "
          "line-broken text, which carry no structure."),
],

"Paragraph length (extractability)": [
    ("p", "Long paragraphs are hard for an engine to quote cleanly - it cannot lift a "
          "single idea without dragging in three others. Short blocks with one idea each "
          "are far more extractable."),
    ("ol", [
        "Break every paragraph over roughly 80 words into smaller ones.",
        "Aim for <b>2-4 sentences</b> per paragraph, one idea each.",
        "Lead each paragraph with its point. Do not save the conclusion for the end.",
        "Use subheadings every 200-300 words to give the engine clear boundaries.",
    ]),
],

"H1 + logical heading structure": [
    ("p", "AI extraction leans heavily on heading hierarchy to understand what a page is "
          "about and which passage answers which question. A missing or duplicated H1, or "
          "headings that jump levels, degrade that understanding."),
    ("ol", [
        "Give every page <b>exactly one H1</b> - the page's main subject.",
        "Nest headings logically: H2 for main sections, H3 for subsections within them. "
        "Never skip from H1 straight to H3.",
        "In Elementor, check the Heading widget's HTML Tag setting on every heading - it "
        "is common for headings to be styled as headings but tagged as paragraphs, or for "
        "several to be set to H1.",
        "Make headings descriptive, ideally phrased as the question they answer.",
    ]),
    ("note", "A frequent Elementor issue: the theme outputs the site title as an H1 on "
             "every page, and the page's real heading is also an H1. Check the theme's "
             "header settings if the audit reports multiple H1s."),
],

"Freshness signals": [
    ("p", "AI engines have a strong recency bias. Undated content, and content that looks "
          "old, loses ground to a competitor's equivalent page that visibly says it was "
          "updated this year."),
    ("ol", [
        "Add a visible <b>\"Last updated: [Month Year]\"</b> line to cornerstone pages and "
        "blog posts.",
        "Ensure the <code>dateModified</code> in your Article schema updates when you edit "
        "a post - Yoast and Rank Math do this if the post is genuinely re-saved.",
        "Include the current year in titles and headings where it is honest to do so "
        "(e.g. \"[Service] costs in [City] - 2026 guide\").",
        "Set a calendar reminder to refresh cornerstone pages <b>quarterly</b>: update "
        "figures, add a new question, refresh the date.",
    ]),
    ("note", "Do not simply bump dates without changing anything. Publishing a new date on "
             "unchanged content is a well-known manipulation and both search and AI "
             "systems are increasingly able to detect it. Make a real change, then update "
             "the date."),
],

# ---------------------------------------------------------------------------
# Entity & Trust
# ---------------------------------------------------------------------------
"About page discoverable": [
    ("p", "The About page is a primary source for the entity profile AI builds about a "
          "business - who runs it, how long it has existed, what it is qualified to do."),
    ("ol", [
        "Create or improve an About page and link it from the main navigation.",
        "Include: founding year, number of staff, named leadership with credentials, "
        "service area, and any licences, certifications, or association memberships.",
        "Use concrete figures - years trading, jobs completed, clients served.",
        "Add Person schema for named leadership if the business trades on individual "
        "expertise (common for law, medicine, and consultancy).",
    ]),
],

"Contact info (NAP) present": [
    ("p", "NAP - Name, Address, Phone - is how AI corroborates that a business is real "
          "and physically located where it claims. Inconsistent or missing NAP weakens "
          "every local AI answer that might have named you."),
    ("ol", [
        "Show the full business name, street address, and phone number in the site "
        "<b>footer</b>, so they appear on every page.",
        "Create a proper Contact page with the same details, plus opening hours and an "
        "embedded map.",
        "Make sure these details match <b>exactly</b> - character for character - what "
        "appears in your Organization schema, your Google Business Profile, and any "
        "directory listing. \"Suite 200\" and \"Ste. 200\" are different strings to a machine.",
    ]),
    ("note", "Consistency matters more than format. Pick one way of writing the address "
             "and use it literally everywhere."),
],

"Author attribution on content": [
    ("p", "AI engines weigh expertise and authorship when deciding which sources to trust. "
          "Anonymous content - or content bylined 'admin' - is a weak signal, particularly "
          "in regulated fields."),
    ("ol", [
        "Give every blog post and guide a real, named author.",
        "Create an author bio page for each writer with their credentials, experience, and "
        "photo.",
        "Link the byline to the bio page.",
        "Add Person schema to author bios, with <code>jobTitle</code>, "
        "<code>worksFor</code>, and <code>sameAs</code> links to their LinkedIn or "
        "professional profile.",
    ]),
    ("code", "JSON-LD - author bio page",
     '<script type="application/ld+json">\n'
     '{\n'
     '  "@context": "https://schema.org",\n'
     '  "@type": "Person",\n'
     '  "name": "Full Name",\n'
     '  "jobTitle": "Their Role",\n'
     '  "worksFor": { "@type": "Organization", "name": "Your Business Name" },\n'
     '  "url": "https://{domain}/team/full-name",\n'
     '  "sameAs": ["https://www.linkedin.com/in/their-profile"]\n'
     '}\n'
     '</script>'),
],

"Cross-platform entity corroboration": [
    ("p", "AI engines confirm that a business is real by finding it in several places. A "
          "site with no corroborating presence elsewhere looks thin - and in AI search, "
          "the majority of citations come from third-party sources rather than the "
          "brand's own website."),
    ("ol", [
        "<b>Google Business Profile first.</b> Claim it, fill every field, set correct "
        "categories, add photos, populate the Q&amp;A section, and respond to reviews. "
        "This is free and it feeds local AI answers directly.",
        "Link the official social profiles from the site footer.",
        "List the business in relevant directories - and use identical NAP details in "
        "every one.",
        "Add every one of those URLs to the <code>sameAs</code> array in your "
        "Organization schema.",
    ]),
    ("p", "Directories worth the effort, by sector:"),
    ("ul", [
        "<b>Legal</b> - Avvo, Justia, the state bar directory, Martindale",
        "<b>Trades / home services</b> - Yelp, Angi, Houzz, trade association listings",
        "<b>Medical / dental</b> - Healthgrades, the relevant professional college",
        "<b>B2B / agencies</b> - Clutch, G2, LinkedIn company page",
    ]),
    ("note", "Beyond directories: Reddit and YouTube are among the most-cited sources in "
             "AI answers. Genuine participation - answering real questions in relevant "
             "communities, publishing explainer videos with good transcripts - builds "
             "citation surface that a website alone cannot."),
],

}


def guide_for(check_name):
    """Return the step list for a check, or None if we have no guide."""
    return GUIDES.get(check_name)
