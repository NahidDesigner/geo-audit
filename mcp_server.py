#!/usr/bin/env python3
"""
MCP server for the GEO Audit dashboard.

Implements the Streamable HTTP transport (MCP spec 2025-11-25) as a plain
JSON-RPC endpoint mounted inside the existing Flask app - no extra service,
no SDK dependency, no long-lived connections.

Auth: the endpoint URL carries a secret token (/mcp/<MCP_TOKEN>). Claude.ai
custom connectors send no bearer header for token-less servers, so the secret
lives in the path. Treat the URL like a password.

Exposed tools let you run and inspect audits, and manage prospecting, from a
Claude chat.
"""

PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "run_audit",
        "description": (
            "Run an AI visibility (GEO) audit on a website. Scores how well AI "
            "engines like ChatGPT, Claude, Perplexity and Google AI Overviews can "
            "access, understand and cite the site. Returns the score, grade, and "
            "the issues found, ordered by business impact. Takes 15-60 seconds."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "Website to audit, e.g. example.com"},
                "max_pages": {"type": "integer", "default": 8,
                              "description": "Pages to sample (2-15). Default 8."},
                "brand": {"type": "string",
                          "description": "Brand name shown on the report. "
                                         "Defaults to the client's brand, or the "
                                         "dashboard's brand."},
                "client": {"type": "string",
                           "description": "Client/workspace name to file this audit "
                                          "under, e.g. 'Bosseo'. Must already exist "
                                          "(see list_clients). Omit for unassigned."},
                "deep": {"type": "boolean", "default": False,
                         "description": "Deep scan: adds an LLM judge's verdict on "
                                        "citation potential and customer-question "
                                        "coverage. One LLM API call, ~10s slower. "
                                        "Requires LLM_PROVIDER configured."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_audits",
        "description": ("List recent audits with their scores, grades and status. "
                        "Use to see audit history or find an audit_id."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 15,
                          "description": "How many to return (1-50)."},
                "client": {"type": "string",
                           "description": "Only show audits for this client."},
            },
        },
    },
    {
        "name": "get_audit",
        "description": ("Get the full detail of one audit: every check, its status, "
                        "points, what was found, the impact level, and the "
                        "recommended fix. Use this to discuss or act on results."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audit_id": {"type": "integer",
                             "description": "The audit's id (from list_audits)."},
            },
            "required": ["audit_id"],
        },
    },
    {
        "name": "get_report_links",
        "description": ("Get shareable links to the three report variants for an "
                        "audit: internal (with fixes), client (findings only), and "
                        "the remediation guide (sellable step-by-step deliverable). "
                        "Only works if PUBLIC_URL is configured."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "audit_id": {"type": "integer"},
            },
            "required": ["audit_id"],
        },
    },
    {
        "name": "find_prospects",
        "description": ("Search Google Places for businesses matching a query and add "
                        "them to the prospect list. Only businesses with a website are "
                        "added, deduplicated by domain. Requires GOOGLE_PLACES_API_KEY."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "e.g. 'personal injury lawyers in Denver, CO'"},
                "max_results": {"type": "integer", "default": 10,
                                "description": "1-60. Default 10."},
                "client": {"type": "string",
                           "description": "Client/workspace to file these prospects "
                                          "under. Deduplication is per-client."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_prospects",
        "description": ("List prospects with their status (found / auditing / audited / "
                        "drafted / sent), score, contact email, and draft subject."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "description": "Optional filter: found, audited, drafted, sent, error."},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "process_prospects",
        "description": (
            "For each given prospect: audit their site, find a contact email, and have "
            "the configured LLM draft outreach based on the real findings. Drafts only "
            "- this never sends email. Runs in the background; poll with list_prospects."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prospect_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Prospect ids. Omit to process all 'found' prospects (max 25)."},
                "max_pages": {"type": "integer", "default": 6},
            },
        },
    },
    {
        "name": "get_prospect_draft",
        "description": ("Read the AI-drafted outreach email for a prospect, so you can "
                        "review or rewrite it before sending."),
        "inputSchema": {
            "type": "object",
            "properties": {"prospect_id": {"type": "integer"}},
            "required": ["prospect_id"],
        },
    },
    {
        "name": "update_prospect_draft",
        "description": ("Rewrite a prospect's outreach draft (subject, body, and/or "
                        "recipient). Does not send - saves the draft for review in the "
                        "dashboard."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "integer"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "to": {"type": "string", "description": "Recipient email address."},
            },
            "required": ["prospect_id"],
        },
    },
    {
        "name": "list_clients",
        "description": ("List client workspaces. Audits and prospects can be filed "
                        "under a client to keep each client's sites separate."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_client",
        "description": ("Create a client workspace. Optionally set a brand, which "
                        "white-labels the reports generated for that client."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "e.g. 'Bosseo'"},
                "brand": {"type": "string",
                          "description": "Optional. Brand shown on this client's reports."},
                "notes": {"type": "string", "description": "Optional."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "assign_audit",
        "description": "Move an existing audit into a client workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "audit_id": {"type": "integer"},
                "client": {"type": "string",
                           "description": "Client name. Empty string unassigns it."},
            },
            "required": ["audit_id", "client"],
        },
    },
]


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text(s):
    """MCP tool result: a single text content block."""
    return {"content": [{"type": "text", "text": s}], "isError": False}


def _fail(s):
    return {"content": [{"type": "text", "text": s}], "isError": True}
