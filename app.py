#!/usr/bin/env python3
"""
GEO Audit Dashboard
===================
Web interface for the AI Visibility Audit tool. Run audits from the browser,
view reports, download PDFs. Designed for single-operator use on a VPS.

Configuration (environment variables, set by the systemd service):
    GEO_PASSWORD        login password              (required)
    GEO_BRAND           default brand on reports    (default: "AI Visibility Audit")
    GEO_PORT            port to listen on           (default: 8080)
    GEO_DATA_DIR        where reports/db live       (default: ./data)
    GEO_ALLOW_PRIVATE   set to 1 to allow auditing localhost/private IPs (testing only)
"""

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template_string, request, send_file, session, url_for)

from geo_audit import run_audit

# ----------------------------------------------------------------------------
# Config & storage
# ----------------------------------------------------------------------------

DATA_DIR = os.environ.get("GEO_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
REPORT_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORT_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "audits.db")
PASSWORD = os.environ.get("GEO_PASSWORD", "")
if not PASSWORD:
    raise RuntimeError(
        "GEO_PASSWORD is not set. Add it in Coolify -> your app -> "
        "Environment Variables, then redeploy.")
DEFAULT_BRAND = os.environ.get("GEO_BRAND", "AI Visibility Audit")
ALLOW_PRIVATE = os.environ.get("GEO_ALLOW_PRIVATE") == "1"

SECRET_FILE = os.path.join(DATA_DIR, ".secret_key")
if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
with open(SECRET_FILE) as f:
    SECRET_KEY = f.read().strip()

app = Flask(__name__)
app.secret_key = SECRET_KEY

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            brand TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            brand TEXT NOT NULL,
            max_pages INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            score INTEGER,
            grade TEXT,
            pdf INTEGER DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id TEXT,
            name TEXT NOT NULL,
            website TEXT NOT NULL,
            domain TEXT NOT NULL,
            address TEXT,
            email TEXT,
            status TEXT NOT NULL DEFAULT 'found',
            audit_id INTEGER,
            score INTEGER,
            grade TEXT,
            email_subject TEXT,
            email_body TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )""")

        # --- migrations: add client_id to existing installs without losing data
        for table in ("audits", "prospects"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "client_id" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN client_id INTEGER")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospect_domain ON prospects(domain)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_client ON audits(client_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospect_client ON prospects(client_id)")
        # prospect dedupe is per-client: two clients may legitimately target the
        # same business, so a global unique index would be wrong.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospect_client_domain "
                     "ON prospects(client_id, domain)")
        conn.execute("""CREATE TABLE IF NOT EXISTS presence_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            engine TEXT NOT NULL,
            prompt TEXT NOT NULL,
            result INTEGER NOT NULL,      -- 3 recommended / 2 mentioned / 1 sources-only / 0 absent
            notes TEXT,
            tested_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ptest_client "
                     "ON presence_tests(client_id, engine, prompt)")
        conn.execute("""CREATE TABLE IF NOT EXISTS manual_results (
            audit_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            status TEXT NOT NULL,          -- pass | warn | fail
            notes TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (audit_id, key)
        )""")


init_db()

# ---------------------------------------------------------------------------
# Prospecting / outreach configuration (all optional; features disable
# themselves cleanly when unconfigured)
# ---------------------------------------------------------------------------
import prospects as P

PLACES_KEY   = os.environ.get("GOOGLE_PLACES_API_KEY", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "")          # openai | gemini | anthropic
LLM_KEY      = os.environ.get("LLM_API_KEY", "")
LLM_MODEL    = os.environ.get("LLM_MODEL", "") or None
SMTP_CFG = {
    "host": os.environ.get("SMTP_HOST", ""),
    "port": os.environ.get("SMTP_PORT", "587"),
    "user": os.environ.get("SMTP_USER", ""),
    "password": os.environ.get("SMTP_PASS", ""),
    "from_addr": os.environ.get("SMTP_FROM", ""),
    "sender_name": os.environ.get("OUTREACH_SENDER_NAME", ""),
}
SENDER_NAME      = os.environ.get("OUTREACH_SENDER_NAME", "Me")
PHYSICAL_ADDRESS = os.environ.get("OUTREACH_ADDRESS", "")
AUTO_SEND_MASTER = os.environ.get("AUTO_SEND", "0") == "1"
SEND_DAILY_CAP   = int(os.environ.get("SEND_DAILY_CAP", "10"))

PLACES_OK = bool(PLACES_KEY)
LLM_OK    = bool(LLM_PROVIDER and LLM_KEY)
SMTP_OK   = bool(SMTP_CFG["host"] and SMTP_CFG["from_addr"])


def _sent_today():
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM prospects WHERE status='sent' "
            "AND sent_at LIKE ?", (datetime.now().strftime("%Y-%m-%d") + "%",)
        ).fetchone()
    return row["c"]


# ---------------------------------------------------------------------------
# Clients (workspaces) - keep each client's sites in their own tab
# ---------------------------------------------------------------------------

def all_clients():
    with db() as conn:
        return conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM audits a WHERE a.client_id=c.id) audit_count "
            "FROM clients c ORDER BY c.name COLLATE NOCASE").fetchall()


def get_client(cid):
    if not cid:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()


def client_brand(cid):
    """A client can override the brand shown on their reports (white-labelling)."""
    c = get_client(cid)
    if c and c["brand"]:
        return c["brand"]
    return DEFAULT_BRAND


def _current_client():
    """Selected client from ?client=N. 0/absent means 'Unassigned'; -1 means All."""
    raw = request.args.get("client")
    if raw in (None, ""):
        return None            # All
    try:
        v = int(raw)
    except ValueError:
        return None
    return v                   # 0 = unassigned, N = that client


def _client_filter(cid):
    """SQL fragment + params for filtering by the selected client."""
    if cid is None:
        return "", ()
    if cid == 0:
        return " WHERE client_id IS NULL", ()
    return " WHERE client_id=?", (cid,)


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if PASSWORD and secrets.compare_digest(request.form.get("password", ""), PASSWORD):
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Wrong password."
    return render_template_string(LOGIN_TMPL, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------------------
# SSRF guard - don't let the audit fetch internal/private addresses
# ----------------------------------------------------------------------------

def host_is_public(url):
    if ALLOW_PRIVATE:
        return True
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    return True


@app.route("/clients", methods=["GET", "POST"])
@login_required
def clients_page():
    msg = ""
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        brand = (request.form.get("brand") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        if name:
            try:
                with _db_lock, db() as conn:
                    conn.execute(
                        "INSERT INTO clients (name,brand,notes,created_at) "
                        "VALUES (?,?,?,?)",
                        (name, brand or None, notes or None,
                         datetime.now().strftime("%Y-%m-%d %H:%M")))
                msg = f"Client '{name}' created."
            except sqlite3.IntegrityError:
                msg = f"A client named '{name}' already exists."
    return render_template_string(CLIENTS_TMPL, clients=all_clients(), msg=msg,
                                  default_brand=DEFAULT_BRAND)


@app.route("/clients/<int:cid>/edit", methods=["POST"])
@login_required
def client_edit(cid):
    name = (request.form.get("name") or "").strip()
    brand = (request.form.get("brand") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    if name:
        with _db_lock, db() as conn:
            conn.execute("UPDATE clients SET name=?, brand=?, notes=? WHERE id=?",
                         (name, brand or None, notes or None, cid))
    return redirect(url_for("clients_page"))


@app.route("/clients/<int:cid>/delete", methods=["POST"])
@login_required
def client_delete(cid):
    """Delete the client only. Their audits/prospects are kept and become
    unassigned - deleting a workspace should never destroy audit history."""
    with _db_lock, db() as conn:
        conn.execute("UPDATE audits SET client_id=NULL WHERE client_id=?", (cid,))
        conn.execute("UPDATE prospects SET client_id=NULL WHERE client_id=?", (cid,))
        conn.execute("DELETE FROM clients WHERE id=?", (cid,))
    return redirect(url_for("clients_page"))


@app.route("/audits/<int:audit_id>/assign", methods=["POST"])
@login_required
def audit_assign(audit_id):
    raw = request.form.get("client_id") or ""
    cid = int(raw) if raw.isdigit() and int(raw) > 0 else None
    with _db_lock, db() as conn:
        conn.execute("UPDATE audits SET client_id=? WHERE id=?", (cid, audit_id))
    return redirect(request.referrer or url_for("index"))


# ----------------------------------------------------------------------------
# Audit runner (background thread)
# ----------------------------------------------------------------------------

def _deep_llm_cfg():
    """LLM config for deep scans, or None when not configured."""
    if not LLM_OK:
        return None
    return {"provider": LLM_PROVIDER, "api_key": LLM_KEY, "model": LLM_MODEL}


def run_audit_job(audit_id, url, brand, max_pages, deep=False):
    out_base = os.path.join(REPORT_DIR, str(audit_id))
    try:
        result = run_audit(url, brand, max_pages, out_base,
                           deep_llm=_deep_llm_cfg() if deep else None)
        with _db_lock, db() as conn:
            conn.execute(
                "UPDATE audits SET status='done', score=?, grade=?, pdf=? WHERE id=?",
                (result["score"], result["grade"], int(result["pdf"]), audit_id))
    except Exception as e:
        traceback.print_exc()
        with _db_lock, db() as conn:
            conn.execute("UPDATE audits SET status='error', error=? WHERE id=?",
                         (str(e)[:500], audit_id))


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@app.route("/", methods=["GET"])
@login_required
def index():
    cid = _current_client()
    where, params = _client_filter(cid)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM audits{where} ORDER BY id DESC LIMIT 200",
            params).fetchall()
        counts = {
            "all": conn.execute("SELECT COUNT(*) c FROM audits").fetchone()["c"],
            "unassigned": conn.execute(
                "SELECT COUNT(*) c FROM audits WHERE client_id IS NULL"
            ).fetchone()["c"],
        }
    running = any(r["status"] == "running" for r in rows)
    return render_template_string(
        INDEX_TMPL, rows=rows, running=running,
        default_brand=client_brand(cid) if cid else DEFAULT_BRAND,
        clients=all_clients(), cid=cid, counts=counts, llm_ok=LLM_OK,
        sel_client=get_client(cid) if cid else None)


@app.route("/run", methods=["POST"])
@login_required
def run():
    url = (request.form.get("url") or "").strip()
    raw_cid = request.form.get("client_id") or ""
    cid = int(raw_cid) if raw_cid.isdigit() and int(raw_cid) > 0 else None
    brand = (request.form.get("brand") or "").strip() or client_brand(cid)
    try:
        max_pages = max(2, min(int(request.form.get("max_pages", 6)), 25))
    except ValueError:
        max_pages = 6
    if not url:
        return redirect(url_for("index", client=raw_cid or None))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not host_is_public(url):
        with db() as conn:
            conn.execute("INSERT INTO audits (url,brand,max_pages,status,error,"
                         "created_at,client_id) VALUES (?,?,?,?,?,?,?)",
                         (url, brand, max_pages, "error",
                          "Host is not a public website (or DNS failed).",
                          datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
        return redirect(url_for("index", client=raw_cid or None))

    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO audits (url,brand,max_pages,status,created_at,client_id) "
            "VALUES (?,?,?,?,?,?)",
            (url, brand, max_pages, "running",
             datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
        audit_id = cur.lastrowid
    deep = request.form.get("deep") == "1"
    threading.Thread(target=run_audit_job,
                     args=(audit_id, url, brand, max_pages, deep),
                     daemon=True).start()
    return redirect(url_for("index", client=raw_cid or None))


VARIANT_SUFFIX = {"internal": "", "client": "-client", "guide": "-guide"}


def _report_path(audit_id, ext, variant="internal"):
    suffix = VARIANT_SUFFIX.get(variant, "")
    p = os.path.join(REPORT_DIR, f"{audit_id}{suffix}.{ext}")
    return p if os.path.exists(p) else None


@app.route("/report/<int:audit_id>")
@app.route("/report/<int:audit_id>/<variant>")
@login_required
def report(audit_id, variant="internal"):
    p = _report_path(audit_id, "html", variant)
    if not p:
        abort(404)
    with open(p, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.route("/pdf/<int:audit_id>")
@app.route("/pdf/<int:audit_id>/<variant>")
@login_required
def pdf(audit_id, variant="internal"):
    p = _report_path(audit_id, "pdf", variant)
    if not p:
        abort(404)
    with db() as conn:
        row = conn.execute("SELECT url FROM audits WHERE id=?", (audit_id,)).fetchone()
    site = urlparse(row["url"]).netloc.replace(":", "_") if row else "site"
    tag = {"internal": "-INTERNAL", "client": "",
           "guide": "-remediation-guide"}.get(variant, "")
    return send_file(p, as_attachment=True,
                     download_name=f"ai-visibility-audit-{site}{tag}.pdf")


@app.route("/download-zip/<variant>")
@login_required
def download_zip(variant):
    """ZIP of ONE report variant (internal or client) for every completed audit
    in the current view (respects ?client=). Separate buttons per variant so the
    client-safe set and the internal-with-fixes set never mix in one download.
    The remediation guide is never bulk-exported - it's the paid deliverable."""
    import io
    import zipfile

    if variant not in ("internal", "client"):
        abort(404)

    cid = _current_client()
    where, params = _client_filter(cid)
    base = f"SELECT id, url FROM audits{where}"
    glue = " AND " if where else " WHERE "
    with db() as conn:
        rows = conn.execute(
            base + glue + "status='done' AND pdf=1 ORDER BY id", params
        ).fetchall()
    if not rows:
        abort(404)

    tag = "-INTERNAL" if variant == "internal" else ""
    buf = io.BytesIO()
    added, seen = 0, {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            domain = (urlparse(r["url"]).netloc or f"audit-{r['id']}")
            domain = domain.removeprefix("www.").replace(":", "_")
            if domain in seen:
                domain = f"{domain}-{r['id']}"
            seen[domain] = True
            p = _report_path(r["id"], "pdf", variant)
            if p:
                z.write(p, f"{domain}{tag}.pdf")
                added += 1
    if not added:
        abort(404)
    buf.seek(0)

    c = get_client(cid) if cid else None
    label = c["name"].lower().replace(" ", "-") if c else (
        "unassigned" if cid == 0 else "all")
    stamp = datetime.now().strftime("%Y-%m-%d")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"ai-visibility-{variant}-{label}-{stamp}.zip")


@app.route("/delete/<int:audit_id>", methods=["POST"])
@login_required
def delete(audit_id):
    for ext in ("html", "pdf", "json"):
        for variant in VARIANT_SUFFIX:
            p = _report_path(audit_id, ext, variant)
            if p:
                os.remove(p)
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM audits WHERE id=?", (audit_id,))
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# AI Tests - manual presence testing across AI engines
#
# Automated testing of consumer AI surfaces (ChatGPT, Gemini, AI Overviews)
# is not feasible: there is no API for those experiences and simulating them
# would be dishonest. Instead: you run standard prompts in the real engines,
# record what happened, and the app scores it. Human-run, machine-scored.
# ---------------------------------------------------------------------------

ENGINES = {
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "claude": "Claude",
    "ai_overviews": "Google AI Overviews",
    "ai_mode": "Google AI Mode",
    "copilot": "Bing Copilot",
}

RESULT_LABELS = {
    3: "Recommended by name",
    2: "Mentioned / cited among others",
    1: "In sources only / after follow-up",
    0: "Not present",
}


def promptpack(service, city, business):
    """The standard test prompts for a local business. Same pack every time,
    so scores are comparable between businesses and across months."""
    s, c, b = service.strip(), city.strip(), business.strip()
    out = [
        f"best {s} in {c}",
        f"who is the best {s} in {c}? give me 2-3 names",
        f"I need a {s} in {c} - who should I contact?",
        f"how much does a {s} cost in {c}?",
        f"compare the top {s} options in {c}",
        f"what should I look for when hiring a {s} in {c}?",
    ]
    if b:
        out += [f"is {b} in {c} legit? what do reviews say?",
                f"tell me about {b} in {c}"]
    return out


def presence_scores(cid):
    """Score from the LATEST entry per (engine, prompt) in this client scope.
    Re-testing a prompt supersedes the old result; history rows are kept."""
    where, params = _client_filter(cid)
    with db() as conn:
        rows = conn.execute(
            f"""SELECT p.* FROM presence_tests p
                JOIN (SELECT engine, prompt, MAX(id) mid FROM presence_tests
                      {where} GROUP BY engine, prompt) latest
                ON p.id = latest.mid
                ORDER BY p.engine, p.id""", params).fetchall()
    if not rows:
        return None
    overall_e = sum(r["result"] for r in rows)
    overall_m = 3 * len(rows)
    per_engine = {}
    for r in rows:
        e = per_engine.setdefault(r["engine"], {"earned": 0, "max": 0, "n": 0})
        e["earned"] += r["result"]
        e["max"] += 3
        e["n"] += 1
    for e in per_engine.values():
        e["pct"] = round(100 * e["earned"] / e["max"])
    return {"pct": round(100 * overall_e / overall_m), "tests": len(rows),
            "per_engine": per_engine, "latest_rows": rows}


@app.route("/aitests")
@login_required
def aitests_page():
    cid = _current_client()
    where, params = _client_filter(cid)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM presence_tests{where} ORDER BY id DESC LIMIT 300",
            params).fetchall()
    svc = request.args.get("svc", "")
    city = request.args.get("city", "")
    biz = request.args.get("biz", "")
    pack = promptpack(svc, city, biz) if (svc and city) else []
    return render_template_string(
        AITESTS_TMPL, rows=rows, clients=all_clients(), cid=cid,
        sel_client=get_client(cid) if cid else None,
        scores=presence_scores(cid), engines=ENGINES, labels=RESULT_LABELS,
        pack=pack, svc=svc, city=city, biz=biz)


@app.route("/aitests/add", methods=["POST"])
@login_required
def aitests_add():
    raw_cid = request.form.get("client_id") or ""
    cid = int(raw_cid) if raw_cid.isdigit() and int(raw_cid) > 0 else None
    engine = request.form.get("engine", "")
    prompt = (request.form.get("prompt") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    try:
        result = int(request.form.get("result", -1))
    except ValueError:
        result = -1
    if engine in ENGINES and prompt and result in RESULT_LABELS:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO presence_tests (client_id,engine,prompt,result,"
                "notes,tested_at) VALUES (?,?,?,?,?,?)",
                (cid, engine, prompt, result, notes or None,
                 datetime.now().strftime("%Y-%m-%d %H:%M")))
    return redirect(url_for("aitests_page", client=raw_cid or None))


@app.route("/aitests/delete/<int:tid>", methods=["POST"])
@login_required
def aitests_delete(tid):
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM presence_tests WHERE id=?", (tid,))
    return redirect(request.referrer or url_for("aitests_page"))


# ---------------------------------------------------------------------------
# Manual checks per audit (off-site verification) + report re-render
# ---------------------------------------------------------------------------
import manual_audit as MA


def _manual_items(audit_id):
    with db() as conn:
        rows = conn.execute("SELECT * FROM manual_results WHERE audit_id=?",
                            (audit_id,)).fetchall()
    return MA.merged({r["key"]: r for r in rows})


def re_render_reports(audit_id):
    """Rebuild the three HTML/PDF variants from the stored audit JSON plus the
    current manual results. The automatic score is untouched."""
    from geo_audit import Check, build_html
    p = os.path.join(REPORT_DIR, f"{audit_id}.json")
    if not os.path.exists(p):
        return False
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    checks = [Check(**{k: c.get(k) for k in
                       ("category", "name", "status", "points", "max_points",
                        "detail", "fix", "impact", "why")})
              for c in raw["checks"]]
    with db() as conn:
        row = conn.execute("SELECT url, brand FROM audits WHERE id=?",
                           (audit_id,)).fetchone()
    if not row:
        return False
    site = urlparse(row["url"]).netloc
    manual = _manual_items(audit_id)
    base = os.path.join(REPORT_DIR, str(audit_id))
    variants = [(f"{base}.html", dict(internal=True)),
                (f"{base}-client.html", dict(internal=False)),
                (f"{base}-guide.html", dict(internal=False, guide=True))]
    for path, kw in variants:
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_html(site, row["brand"], checks, raw["data"],
                               manual=manual, **kw))
    try:
        import pdf_render
        for path, _ in variants:
            pdf_render.html_to_pdf(path, path.replace(".html", ".pdf"))
    except Exception:
        traceback.print_exc()
    return True


@app.route("/audit/<int:audit_id>/manual", methods=["GET", "POST"])
@login_required
def manual_page(audit_id):
    with db() as conn:
        a = conn.execute("SELECT * FROM audits WHERE id=?", (audit_id,)).fetchone()
    if not a or a["status"] != "done":
        abort(404)
    msg = ""
    if request.method == "POST":
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with _db_lock, db() as conn:
            for key in MA.CATALOG:
                status = request.form.get(f"status-{key}", "")
                notes = (request.form.get(f"notes-{key}") or "").strip()
                if status in ("pass", "warn", "fail"):
                    conn.execute(
                        "INSERT INTO manual_results (audit_id,key,status,notes,"
                        "updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(audit_id,key) DO UPDATE SET status=excluded.status, "
                        "notes=excluded.notes, updated_at=excluded.updated_at",
                        (audit_id, key, status, notes or None, now))
                elif status == "clear":
                    conn.execute("DELETE FROM manual_results WHERE audit_id=? "
                                 "AND key=?", (audit_id, key))
        re_render_reports(audit_id)
        msg = "Saved - reports regenerated with the manual results."
    items = _manual_items(audit_id)
    e_earn, e_max = MA.extended_score(items)
    return render_template_string(MANUAL_TMPL, a=a, items=items, msg=msg,
                                  e_earn=e_earn, e_max=e_max,
                                  site=urlparse(a["url"]).netloc)


# ----------------------------------------------------------------------------
# Prospecting: worker + routes
# ----------------------------------------------------------------------------

def _upd(pid, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, db() as conn:
        conn.execute(f"UPDATE prospects SET {sets} WHERE id=?",
                     (*fields.values(), pid))


def process_prospect(pid, brand, max_pages, auto_send):
    """Full chain for one prospect: audit -> harvest email -> LLM draft ->
    optionally send. Each stage updates status so the UI shows progress."""
    with db() as conn:
        p = conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not p:
        return
    try:
        # 1) audit (also creates a normal audit row so it appears in Audits tab)
        _upd(pid, status="auditing")
        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO audits (url,brand,max_pages,status,created_at,client_id) "
                "VALUES (?,?,?,?,?,?)",
                (p["website"], brand, max_pages, "running",
                 datetime.now().strftime("%Y-%m-%d %H:%M"), p["client_id"]))
            audit_id = cur.lastrowid
        out_base = os.path.join(REPORT_DIR, str(audit_id))
        try:
            result = run_audit(p["website"], brand, max_pages, out_base)
            with _db_lock, db() as conn:
                conn.execute("UPDATE audits SET status='done', score=?, grade=?, "
                             "pdf=? WHERE id=?",
                             (result["score"], result["grade"],
                              int(result["pdf"]), audit_id))
        except Exception as e:
            with _db_lock, db() as conn:
                conn.execute("UPDATE audits SET status='error', error=? WHERE id=?",
                             (str(e)[:400], audit_id))
            raise
        _upd(pid, audit_id=audit_id, score=result["score"], grade=result["grade"],
             status="audited")

        # 2) harvest a contact email from their site
        email = P.harvest_email(p["website"])
        _upd(pid, email=email)

        # 3) LLM draft
        if LLM_OK:
            issues, score, grade = P.top_issues_from_json(out_base + ".json")
            draft = P.generate_email(LLM_PROVIDER, LLM_KEY, {
                "name": p["name"], "address": p["address"] or "",
                "website": p["website"], "score": score, "grade": grade,
                "issues": issues, "sender_name": SENDER_NAME, "brand": DEFAULT_BRAND,
            }, model=LLM_MODEL)
            _upd(pid, email_subject=draft["subject"], email_body=draft["body"],
                 status="drafted")
        else:
            _upd(pid, status="audited",
                 error="LLM not configured - no draft generated")
            return

        # 4) optional auto-send (master env switch AND per-batch toggle,
        #    recipient found, daily cap not exceeded)
        if auto_send and AUTO_SEND_MASTER and SMTP_OK and email:
            if _sent_today() >= SEND_DAILY_CAP:
                _upd(pid, error=f"Daily send cap ({SEND_DAILY_CAP}) reached - left as draft")
                return
            body = draft["body"] + P.compliance_footer(
                SENDER_NAME, DEFAULT_BRAND, PHYSICAL_ADDRESS)
            P.send_email(SMTP_CFG, email, draft["subject"], body)
            _upd(pid, status="sent",
                 sent_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
    except Exception as e:
        traceback.print_exc()
        _upd(pid, status="error", error=str(e)[:400])


def process_batch(pids, brand, max_pages, auto_send):
    for pid in pids:
        process_prospect(pid, brand, max_pages, auto_send)


@app.route("/prospects")
@login_required
def prospects_page():
    cid = _current_client()
    where, params = _client_filter(cid)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM prospects{where} ORDER BY id DESC LIMIT 300",
            params).fetchall()
    running = any(r["status"] == "auditing" for r in rows)
    return render_template_string(
        PROSPECTS_TMPL, rows=rows, running=running,
        places_ok=PLACES_OK, llm_ok=LLM_OK, smtp_ok=SMTP_OK,
        auto_send_master=AUTO_SEND_MASTER, cap=SEND_DAILY_CAP,
        sent_today=_sent_today(), default_brand=DEFAULT_BRAND,
        clients=all_clients(), cid=cid,
        sel_client=get_client(cid) if cid else None)


@app.route("/prospects/search", methods=["POST"])
@login_required
def prospects_search():
    query = (request.form.get("query") or "").strip()
    raw_cid = request.form.get("client_id") or ""
    cid = int(raw_cid) if raw_cid.isdigit() and int(raw_cid) > 0 else None
    if not query or not PLACES_OK:
        return redirect(url_for("prospects_page", client=raw_cid or None))
    try:
        max_r = max(1, min(int(request.form.get("max_results", 20)), 60))
    except ValueError:
        max_r = 20
    try:
        results = P.search_places(query, PLACES_KEY, max_r)
    except Exception as e:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO prospects (name,website,domain,status,error,created_at,"
                "client_id) VALUES (?,?,?,?,?,?,?)",
                (f"Search failed: {query}", "-", "-", "error", str(e)[:300],
                 datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
        return redirect(url_for("prospects_page", client=raw_cid or None))

    with _db_lock, db() as conn:
        for r in results:
            domain = urlparse(r["website"]).netloc.lower().removeprefix("www.")
            if not domain:
                continue
            # dedupe within this client's list only - two clients may legitimately
            # target the same business
            if cid is None:
                dup = conn.execute(
                    "SELECT 1 FROM prospects WHERE domain=? AND client_id IS NULL",
                    (domain,)).fetchone()
            else:
                dup = conn.execute(
                    "SELECT 1 FROM prospects WHERE domain=? AND client_id=?",
                    (domain, cid)).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO prospects (place_id,name,website,domain,address,"
                "status,created_at,client_id) VALUES (?,?,?,?,?,?,?,?)",
                (r["place_id"], r["name"], r["website"], domain, r["address"],
                 "found", datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
    return redirect(url_for("prospects_page", client=raw_cid or None))


@app.route("/prospects/process", methods=["POST"])
@login_required
def prospects_process():
    ids = [int(i) for i in request.form.getlist("ids")]
    auto_send = request.form.get("auto_send") == "1"
    raw_cid = request.form.get("client_id") or ""
    cid = int(raw_cid) if raw_cid.isdigit() and int(raw_cid) > 0 else None
    try:
        max_pages = max(2, min(int(request.form.get("max_pages", 6)), 15))
    except ValueError:
        max_pages = 6
    if not ids:
        where = " AND client_id=?" if cid else " AND client_id IS NULL" if raw_cid == "0" else ""
        params = (cid,) if cid else ()
        with db() as conn:
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM prospects WHERE status='found'{where} ORDER BY id",
                params).fetchall()][:25]
    brand = client_brand(cid) if cid else DEFAULT_BRAND
    threading.Thread(target=process_batch,
                     args=(ids, brand, max_pages, auto_send),
                     daemon=True).start()
    return redirect(url_for("prospects_page", client=raw_cid or None))


@app.route("/prospects/email/<int:pid>", methods=["GET", "POST"])
@login_required
def prospect_email(pid):
    with db() as conn:
        p = conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        to_addr = (request.form.get("to") or "").strip()
        _upd(pid, email_subject=subject, email_body=body, email=to_addr or None)
        if action == "send":
            if not SMTP_OK:
                msg = "SMTP is not configured - set SMTP_* environment variables."
            elif not to_addr:
                msg = "No recipient email address."
            elif _sent_today() >= SEND_DAILY_CAP:
                msg = f"Daily send cap ({SEND_DAILY_CAP}) reached."
            else:
                try:
                    full = body + P.compliance_footer(SENDER_NAME, DEFAULT_BRAND,
                                                      PHYSICAL_ADDRESS)
                    P.send_email(SMTP_CFG, to_addr, subject, full)
                    _upd(pid, status="sent",
                         sent_at=datetime.now().strftime("%Y-%m-%d %H:%M"))
                    msg = "Sent."
                except Exception as e:
                    msg = f"Send failed: {e}"
        else:
            msg = "Draft saved."
        with db() as conn:
            p = conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    footer_preview = P.compliance_footer(SENDER_NAME, DEFAULT_BRAND, PHYSICAL_ADDRESS)
    return render_template_string(EMAIL_TMPL, p=p, msg=msg, smtp_ok=SMTP_OK,
                                  footer=footer_preview)


@app.route("/prospects/delete/<int:pid>", methods=["POST"])
@login_required
def prospect_delete(pid):
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM prospects WHERE id=?", (pid,))
    return redirect(url_for("prospects_page"))


# ----------------------------------------------------------------------------
# MCP server (Streamable HTTP) - manage the tool from a Claude chat
# ----------------------------------------------------------------------------
import mcp_server as MCP

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


def _audit_summary(row):
    return {"audit_id": row["id"], "url": row["url"], "status": row["status"],
            "score": row["score"], "grade": row["grade"],
            "created_at": row["created_at"],
            **({"error": row["error"]} if row["error"] else {})}


def _client_id_by_name(name):
    """Resolve a client name to its id. Returns (id, error_message)."""
    name = (name or "").strip()
    if not name:
        return None, None
    with db() as conn:
        r = conn.execute("SELECT id FROM clients WHERE name=? COLLATE NOCASE",
                         (name,)).fetchone()
    if not r:
        with db() as conn:
            avail = [x["name"] for x in conn.execute("SELECT name FROM clients")]
        return None, (f"No client named '{name}'. Existing clients: "
                      f"{', '.join(avail) if avail else '(none)'}. "
                      "Use create_client first.")
    return r["id"], None


def _mcp_call(name, args):
    """Dispatch one MCP tool call. Returns an MCP tool result dict."""

    if name == "run_audit":
        url = (args.get("url") or "").strip()
        if not url:
            return MCP._fail("A url is required.")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not host_is_public(url):
            return MCP._fail("That host is not a reachable public website.")
        cid, err = _client_id_by_name(args.get("client"))
        if err:
            return MCP._fail(err)
        brand = (args.get("brand") or "").strip() or client_brand(cid)
        max_pages = max(2, min(int(args.get("max_pages") or 8), 15))

        with _db_lock, db() as conn:
            cur = conn.execute(
                "INSERT INTO audits (url,brand,max_pages,status,created_at,client_id) "
                "VALUES (?,?,?,?,?,?)",
                (url, brand, max_pages, "running",
                 datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
            aid = cur.lastrowid
        # run synchronously: the chat is waiting for the answer
        try:
            deep = bool(args.get("deep"))
            res = run_audit(url, brand, max_pages,
                            os.path.join(REPORT_DIR, str(aid)),
                            deep_llm=_deep_llm_cfg() if deep else None)
        except Exception as e:
            with _db_lock, db() as conn:
                conn.execute("UPDATE audits SET status='error', error=? WHERE id=?",
                             (str(e)[:400], aid))
            return MCP._fail(f"Audit failed: {e}")
        with _db_lock, db() as conn:
            conn.execute("UPDATE audits SET status='done', score=?, grade=?, pdf=? "
                         "WHERE id=?", (res["score"], res["grade"],
                                        int(res["pdf"]), aid))

        with open(os.path.join(REPORT_DIR, f"{aid}.json"), encoding="utf-8") as f:
            raw = json.load(f)
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        issues = sorted((c for c in raw["checks"] if c["status"] != "pass"),
                        key=lambda c: (order.get(c.get("impact", "medium"), 2),
                                       -c["max_points"]))
        out = {
            "audit_id": aid, "url": url,
            "score": res["score"], "grade": res["grade"],
            "pages_analyzed": res["pages_analyzed"],
            "summary": {
                "passed": sum(1 for c in raw["checks"] if c["status"] == "pass"),
                "needs_work": sum(1 for c in raw["checks"] if c["status"] == "warn"),
                "failed": sum(1 for c in raw["checks"] if c["status"] == "fail"),
            },
            "issues": [{"name": c["name"], "impact": c["impact"],
                        "status": c["status"], "found": c["detail"],
                        "fix": c["fix"]} for c in issues],
        }
        if PUBLIC_URL:
            out["reports"] = {
                "internal": f"{PUBLIC_URL}/report/{aid}",
                "client": f"{PUBLIC_URL}/report/{aid}/client",
                "guide": f"{PUBLIC_URL}/report/{aid}/guide",
            }
        return MCP._text(json.dumps(out, indent=2))

    if name == "list_audits":
        limit = max(1, min(int(args.get("limit") or 15), 50))
        cid, err = _client_id_by_name(args.get("client"))
        if err:
            return MCP._fail(err)
        with db() as conn:
            if cid:
                rows = conn.execute(
                    "SELECT * FROM audits WHERE client_id=? ORDER BY id DESC LIMIT ?",
                    (cid, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audits ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return MCP._text(json.dumps([_audit_summary(r) for r in rows], indent=2))

    if name == "get_audit":
        aid = int(args.get("audit_id") or 0)
        p = os.path.join(REPORT_DIR, f"{aid}.json")
        if not os.path.exists(p):
            return MCP._fail(f"No completed audit with id {aid}.")
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        with db() as conn:
            row = conn.execute("SELECT * FROM audits WHERE id=?", (aid,)).fetchone()
        out = {"audit_id": aid, "url": row["url"] if row else None,
               "score": raw["score"], "grade": raw["grade"],
               "checks": [{"name": c["name"], "category": c["category"],
                           "status": c["status"], "impact": c.get("impact"),
                           "points": f"{c['points']}/{c['max_points']}",
                           "found": c["detail"], "fix": c["fix"],
                           "why_it_matters": c.get("why", "")}
                          for c in raw["checks"]]}
        return MCP._text(json.dumps(out, indent=2))

    if name == "get_report_links":
        aid = int(args.get("audit_id") or 0)
        if not PUBLIC_URL:
            return MCP._fail("PUBLIC_URL is not configured, so links can't be built. "
                             "Set it to the dashboard's public address.")
        if not os.path.exists(os.path.join(REPORT_DIR, f"{aid}.html")):
            return MCP._fail(f"No report for audit {aid}.")
        return MCP._text(json.dumps({
            "internal_with_fixes": f"{PUBLIC_URL}/report/{aid}",
            "client_findings_only": f"{PUBLIC_URL}/report/{aid}/client",
            "remediation_guide": f"{PUBLIC_URL}/report/{aid}/guide",
            "client_pdf": f"{PUBLIC_URL}/pdf/{aid}/client",
            "guide_pdf": f"{PUBLIC_URL}/pdf/{aid}/guide",
            "note": "These require logging in to the dashboard.",
        }, indent=2))

    if name == "find_prospects":
        if not PLACES_OK:
            return MCP._fail("GOOGLE_PLACES_API_KEY is not configured.")
        query = (args.get("query") or "").strip()
        if not query:
            return MCP._fail("A query is required.")
        n = max(1, min(int(args.get("max_results") or 10), 60))
        cid, err = _client_id_by_name(args.get("client"))
        if err:
            return MCP._fail(err)
        try:
            results = P.search_places(query, PLACES_KEY, n)
        except Exception as e:
            return MCP._fail(f"Places search failed: {e}")
        added, skipped = [], 0
        with _db_lock, db() as conn:
            for r in results:
                domain = urlparse(r["website"]).netloc.lower().removeprefix("www.")
                if not domain:
                    continue
                if cid:
                    dup = conn.execute(
                        "SELECT 1 FROM prospects WHERE domain=? AND client_id=?",
                        (domain, cid)).fetchone()
                else:
                    dup = conn.execute(
                        "SELECT 1 FROM prospects WHERE domain=? AND client_id IS NULL",
                        (domain,)).fetchone()
                if dup:
                    skipped += 1
                    continue
                cur = conn.execute(
                    "INSERT INTO prospects (place_id,name,website,domain,address,"
                    "status,created_at,client_id) VALUES (?,?,?,?,?,?,?,?)",
                    (r["place_id"], r["name"], r["website"], domain, r["address"],
                     "found", datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
                added.append({"prospect_id": cur.lastrowid, "name": r["name"],
                              "website": r["website"]})
        return MCP._text(json.dumps(
            {"added": len(added), "skipped_duplicates": skipped,
             "prospects": added}, indent=2))

    if name == "list_prospects":
        limit = max(1, min(int(args.get("limit") or 20), 100))
        status = (args.get("status") or "").strip()
        with db() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM prospects WHERE status=? ORDER BY id DESC LIMIT ?",
                    (status, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM prospects ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
        return MCP._text(json.dumps([{
            "prospect_id": r["id"], "name": r["name"], "domain": r["domain"],
            "status": r["status"], "score": r["score"], "grade": r["grade"],
            "email": r["email"], "draft_subject": r["email_subject"],
            **({"error": r["error"]} if r["error"] else {}),
        } for r in rows], indent=2))

    if name == "process_prospects":
        ids = args.get("prospect_ids") or []
        if not ids:
            with db() as conn:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM prospects WHERE status='found' ORDER BY id"
                ).fetchall()][:25]
        if not ids:
            return MCP._fail("No prospects to process.")
        max_pages = max(2, min(int(args.get("max_pages") or 6), 15))
        # never auto-send from MCP: drafting only
        threading.Thread(target=process_batch,
                         args=([int(i) for i in ids], DEFAULT_BRAND, max_pages, False),
                         daemon=True).start()
        return MCP._text(json.dumps({
            "processing": len(ids), "prospect_ids": ids,
            "note": "Running in the background: audit -> find email -> draft outreach. "
                    "This does NOT send email. Poll list_prospects for progress; each "
                    "prospect takes roughly 30-60 seconds.",
        }, indent=2))

    if name == "get_prospect_draft":
        pid = int(args.get("prospect_id") or 0)
        with db() as conn:
            r = conn.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            return MCP._fail(f"No prospect with id {pid}.")
        return MCP._text(json.dumps({
            "prospect_id": r["id"], "name": r["name"], "website": r["website"],
            "status": r["status"], "score": r["score"], "grade": r["grade"],
            "to": r["email"], "subject": r["email_subject"], "body": r["email_body"],
            "sent_at": r["sent_at"],
        }, indent=2))

    if name == "update_prospect_draft":
        pid = int(args.get("prospect_id") or 0)
        with db() as conn:
            r = conn.execute("SELECT id FROM prospects WHERE id=?", (pid,)).fetchone()
        if not r:
            return MCP._fail(f"No prospect with id {pid}.")
        fields = {}
        if args.get("subject"):
            fields["email_subject"] = args["subject"]
        if args.get("body"):
            fields["email_body"] = args["body"]
        if args.get("to"):
            fields["to_addr"] = args["to"]
        if not fields:
            return MCP._fail("Nothing to update.")
        upd = {}
        if "email_subject" in fields:
            upd["email_subject"] = fields["email_subject"]
        if "email_body" in fields:
            upd["email_body"] = fields["email_body"]
        if "to_addr" in fields:
            upd["email"] = fields["to_addr"]
        _upd(pid, **upd)
        return MCP._text(json.dumps({
            "prospect_id": pid, "updated": list(upd),
            "note": "Draft saved. Review and send it from the dashboard - "
                    "sending is not available through this connector.",
        }, indent=2))

    if name == "list_clients":
        return MCP._text(json.dumps([{
            "name": c["name"], "brand": c["brand"] or DEFAULT_BRAND,
            "audits": c["audit_count"], "notes": c["notes"],
        } for c in all_clients()], indent=2))

    if name == "create_client":
        cname = (args.get("name") or "").strip()
        if not cname:
            return MCP._fail("A client name is required.")
        try:
            with _db_lock, db() as conn:
                conn.execute("INSERT INTO clients (name,brand,notes,created_at) "
                             "VALUES (?,?,?,?)",
                             (cname, (args.get("brand") or "").strip() or None,
                              (args.get("notes") or "").strip() or None,
                              datetime.now().strftime("%Y-%m-%d %H:%M")))
        except sqlite3.IntegrityError:
            return MCP._fail(f"A client named '{cname}' already exists.")
        return MCP._text(json.dumps({"created": cname}, indent=2))

    if name == "assign_audit":
        aid = int(args.get("audit_id") or 0)
        cname = (args.get("client") or "").strip()
        cid = None
        if cname:
            cid, err = _client_id_by_name(cname)
            if err:
                return MCP._fail(err)
        with db() as conn:
            if not conn.execute("SELECT 1 FROM audits WHERE id=?", (aid,)).fetchone():
                return MCP._fail(f"No audit with id {aid}.")
        with _db_lock, db() as conn:
            conn.execute("UPDATE audits SET client_id=? WHERE id=?", (cid, aid))
        return MCP._text(json.dumps(
            {"audit_id": aid, "client": cname or "(unassigned)"}, indent=2))

    if name == "record_ai_test":
        engine = (args.get("engine") or "").lower()
        prompt = (args.get("prompt") or "").strip()
        try:
            result = int(args.get("result", -1))
        except (TypeError, ValueError):
            result = -1
        if engine not in ENGINES:
            return MCP._fail(f"engine must be one of {list(ENGINES)}")
        if not prompt:
            return MCP._fail("A prompt is required.")
        if result not in RESULT_LABELS:
            return MCP._fail("result must be 0-3: 3 recommended by name, "
                             "2 mentioned/cited, 1 sources only, 0 not present")
        cid, err = _client_id_by_name(args.get("client"))
        if err:
            return MCP._fail(err)
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO presence_tests (client_id,engine,prompt,result,"
                "notes,tested_at) VALUES (?,?,?,?,?,?)",
                (cid, engine, prompt, result,
                 (args.get("notes") or "").strip() or None,
                 datetime.now().strftime("%Y-%m-%d %H:%M")))
        s = presence_scores(cid)
        return MCP._text(json.dumps({
            "recorded": {"engine": engine, "prompt": prompt, "result": result,
                         "meaning": RESULT_LABELS[result]},
            "presence_score_now": f"{s['pct']}% across {s['tests']} tests" if s else None,
        }, indent=2))

    if name == "list_ai_tests":
        cid, err = _client_id_by_name(args.get("client"))
        if err:
            return MCP._fail(err)
        where, params = _client_filter(cid if args.get("client") else None)
        with db() as conn:
            rows = conn.execute(
                f"SELECT * FROM presence_tests{where} ORDER BY id DESC LIMIT 100",
                params).fetchall()
        return MCP._text(json.dumps([{
            "id": r["id"], "engine": r["engine"], "prompt": r["prompt"],
            "result": r["result"], "meaning": RESULT_LABELS[r["result"]],
            "notes": r["notes"], "tested_at": r["tested_at"],
        } for r in rows], indent=2))

    if name == "ai_presence_score":
        cid, err = _client_id_by_name(args.get("client"))
        if err:
            return MCP._fail(err)
        s = presence_scores(cid if args.get("client") else None)
        if not s:
            return MCP._fail("No tests recorded yet for that scope.")
        return MCP._text(json.dumps({
            "presence_score_pct": s["pct"], "active_tests": s["tests"],
            "per_engine": {ENGINES.get(k, k): f"{v['pct']}% ({v['n']} tests)"
                           for k, v in s["per_engine"].items()},
            "note": "Score uses the latest entry per (engine, prompt); older "
                    "entries remain as history.",
        }, indent=2))

    return MCP._fail(f"Unknown tool: {name}")


@app.route("/mcp", methods=["POST", "GET", "DELETE"])
@app.route("/mcp/<token>", methods=["POST", "GET", "DELETE"])
def mcp_endpoint(token=None):
    """MCP Streamable HTTP endpoint.

    Two ways to authenticate:
      1. The secret in the path (/mcp/<MCP_TOKEN>) - simple, works with curl,
         Claude Code, and the MCP Inspector.
      2. An OAuth Bearer token - what Claude.ai's custom connector uses, since
         it forces OAuth on every connector.
    """
    if not MCP_TOKEN:
        return Response("Not found", status=404)

    path_ok = bool(token) and secrets.compare_digest(token, MCP_TOKEN)
    if not (path_ok or _bearer_ok()):
        # Point unauthenticated clients at the OAuth metadata so discovery works.
        resp = Response(json.dumps({"error": "unauthorized"}), status=401,
                        mimetype="application/json")
        resp.headers["WWW-Authenticate"] = (
            'Bearer resource_metadata='
            f'"{_base_url()}/.well-known/oauth-protected-resource"')
        return resp

    if request.method in ("GET", "DELETE"):
        # No server-initiated streaming and no sessions to terminate.
        return Response(status=405)

    msg = request.get_json(silent=True) or {}
    rid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # Notifications carry no id and expect no body.
    if rid is None and method.startswith("notifications/"):
        return Response(status=202)

    if method == "initialize":
        return jsonify(MCP._ok(rid, {
            "protocolVersion": MCP.PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "geo-audit", "version": "1.0.0"},
        }))

    if method == "ping":
        return jsonify(MCP._ok(rid, {}))

    if method == "tools/list":
        return jsonify(MCP._ok(rid, {"tools": MCP.TOOLS}))

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            return jsonify(MCP._ok(rid, _mcp_call(name, args)))
        except Exception as e:
            traceback.print_exc()
            return jsonify(MCP._ok(rid, MCP._fail(f"{type(e).__name__}: {e}")))

    return jsonify(MCP._err(rid, -32601, f"Method not found: {method}"))


# ----------------------------------------------------------------------------
# OAuth 2.1 for the MCP connector
#
# Claude.ai's custom-connector flow runs OAuth discovery + Dynamic Client
# Registration against every server and aborts if the .well-known endpoints
# 404 - even for servers that need no auth at all (anthropics/claude-ai-mcp
# #402, #457). So we implement the minimum viable OAuth 2.1 + PKCE + DCR.
#
# The security model is unchanged: the human approving the authorize step must
# know GEO_PASSWORD. Clients are registered dynamically and tokens are stored
# in the same SQLite database.
# ----------------------------------------------------------------------------

def init_oauth_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            redirect_uris TEXT NOT NULL,
            name TEXT,
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_codes (
            code TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            code_challenge TEXT,
            expires_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_tokens (
            token TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")


init_oauth_db()


def _base_url():
    """External base URL. PUBLIC_URL wins; otherwise derive from the request."""
    if PUBLIC_URL:
        return PUBLIC_URL
    return request.url_root.rstrip("/").replace("http://", "https://", 1)


def _oauth_enabled():
    return bool(MCP_TOKEN)


@app.route("/.well-known/oauth-protected-resource")
@app.route("/.well-known/oauth-protected-resource/<path:_p>")
def oauth_protected_resource(_p=None):
    if not _oauth_enabled():
        abort(404)
    base = _base_url()
    return jsonify({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })


@app.route("/.well-known/oauth-authorization-server")
@app.route("/.well-known/oauth-authorization-server/<path:_p>")
@app.route("/.well-known/openid-configuration")
def oauth_authorization_server(_p=None):
    if not _oauth_enabled():
        abort(404)
    base = _base_url()
    return jsonify({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


@app.route("/oauth/register", methods=["POST"])
def oauth_register():
    """Dynamic Client Registration (RFC 7591). Open by design - possession of a
    client_id grants nothing; the authorize step still requires the password."""
    if not _oauth_enabled():
        abort(404)
    body = request.get_json(silent=True) or {}
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris:
        return jsonify({"error": "invalid_redirect_uri"}), 400
    cid = "geo-" + secrets.token_urlsafe(18)
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO oauth_clients (client_id,redirect_uris,name,"
                     "created_at) VALUES (?,?,?,?)",
                     (cid, json.dumps(uris), body.get("client_name", "")[:100],
                      datetime.now().strftime("%Y-%m-%d %H:%M")))
    return jsonify({
        "client_id": cid,
        "redirect_uris": uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }), 201


@app.route("/oauth/authorize", methods=["GET", "POST"])
def oauth_authorize():
    if not _oauth_enabled():
        abort(404)
    args = request.args if request.method == "GET" else request.form
    cid = args.get("client_id", "")
    redirect_uri = args.get("redirect_uri", "")
    state = args.get("state", "")
    challenge = args.get("code_challenge", "")

    with db() as conn:
        client = conn.execute("SELECT * FROM oauth_clients WHERE client_id=?",
                              (cid,)).fetchone()
    if not client:
        return "Unknown client_id", 400
    if redirect_uri not in json.loads(client["redirect_uris"]):
        return "redirect_uri does not match registration", 400

    # Approval = proving you know the dashboard password.
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("password", ""), PASSWORD):
            return render_template_string(
                OAUTH_TMPL, error="Wrong password.", args=args), 401
        session["authed"] = True
    elif not session.get("authed"):
        return render_template_string(OAUTH_TMPL, error="", args=args)

    code = secrets.token_urlsafe(32)
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO oauth_codes (code,client_id,redirect_uri,"
                     "code_challenge,expires_at) VALUES (?,?,?,?,?)",
                     (code, cid, redirect_uri, challenge, time.time() + 600))
    sep = "&" if "?" in redirect_uri else "?"
    url = f"{redirect_uri}{sep}code={code}"
    if state:
        url += f"&state={state}"
    return redirect(url)


@app.route("/oauth/token", methods=["POST"])
def oauth_token():
    if not _oauth_enabled():
        abort(404)
    f = request.form or {}
    code = f.get("code", "")
    verifier = f.get("code_verifier", "")

    with db() as conn:
        row = conn.execute("SELECT * FROM oauth_codes WHERE code=?", (code,)).fetchone()
    if not row:
        return jsonify({"error": "invalid_grant"}), 400
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM oauth_codes WHERE code=?", (code,))
    if row["expires_at"] < time.time():
        return jsonify({"error": "invalid_grant", "error_description": "expired"}), 400

    # PKCE
    if row["code_challenge"]:
        method = f.get("code_challenge_method") or "S256"
        if method == "S256":
            digest = hashlib.sha256(verifier.encode()).digest()
            computed = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        else:
            computed = verifier
        if not secrets.compare_digest(computed, row["code_challenge"]):
            return jsonify({"error": "invalid_grant",
                            "error_description": "PKCE verification failed"}), 400

    token = secrets.token_urlsafe(32)
    with _db_lock, db() as conn:
        conn.execute("INSERT INTO oauth_tokens (token,client_id,created_at) "
                     "VALUES (?,?,?)",
                     (token, row["client_id"],
                      datetime.now().strftime("%Y-%m-%d %H:%M")))
    return jsonify({"access_token": token, "token_type": "Bearer",
                    "scope": "mcp"})


def _bearer_ok():
    """True if the request carries a valid OAuth access token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    tok = auth[7:].strip()
    # the static MCP_TOKEN also works as a bearer, for Claude Code / curl
    if secrets.compare_digest(tok, MCP_TOKEN):
        return True
    with db() as conn:
        return bool(conn.execute("SELECT 1 FROM oauth_tokens WHERE token=?",
                                 (tok,)).fetchone())


# ----------------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------------

BASE_CSS = """
  * { box-sizing:border-box; }
  body { font-family:'Segoe UI',system-ui,Arial,sans-serif; background:#f1f5f9;
         color:#1e293b; margin:0; }
  .wrap { max-width:1000px; margin:0 auto; padding:24px 16px; }
  .topbar { background:linear-gradient(135deg,#0f172a,#1e3a8a); color:#fff;
            padding:18px 0; margin-bottom:24px; }
  .topbar .wrap { display:flex; justify-content:space-between; align-items:center;
                  padding-top:0; padding-bottom:0; }
  .topbar h1 { font-size:19px; margin:0; }
  .topbar a { color:#93c5fd; text-decoration:none; font-size:13px; }
  .tabs a { display:inline-block; padding:5px 13px; border-radius:6px;
            color:#93c5fd; font-size:13px; font-weight:600; }
  .tabs a.on { background:rgba(255,255,255,.14); color:#fff; }
  .tabs a:hover { color:#fff; }
  .clientbar { display:flex; gap:6px; flex-wrap:wrap; align-items:center;
               margin-bottom:18px; }
  .cbtn { display:inline-block; padding:6px 14px; border-radius:20px; font-size:13px;
          font-weight:600; text-decoration:none; color:#475569; background:#fff;
          border:1px solid #e2e8f0; white-space:nowrap; }
  .cbtn:hover { border-color:#94a3b8; }
  .cbtn.on { background:#0f172a; color:#fff; border-color:#0f172a; }
  .cbtn .n { opacity:.6; font-weight:400; margin-left:4px; }
  .cbtn.manage { color:#1d4ed8; border-style:dashed; }
  .cbtn.dlall { font-weight:700; }
  .cbtn.dlall.internal { margin-left:auto; color:#b45309; background:#fffbeb;
                         border-color:#fde68a; }
  .cbtn.dlall.internal:hover { background:#fef3c7; border-color:#fcd34d; }
  .cbtn.dlall.client { color:#15803d; background:#f0fdf4; border-color:#bbf7d0; }
  .cbtn.dlall.client:hover { background:#dcfce7; border-color:#86efac; }
  .ctx { background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px;
         padding:9px 14px; margin-bottom:14px; font-size:13px; color:#1e3a8a; }
  select { width:100%; padding:10px 12px; border:1px solid #cbd5e1;
           border-radius:8px; font-size:14px; background:#fff; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:12px;
          padding:20px; margin-bottom:20px; }
  label { display:block; font-size:12px; font-weight:700; color:#475569;
          margin:0 0 4px; }
  input[type=text], input[type=password], input[type=number] {
      width:100%; padding:10px 12px; border:1px solid #cbd5e1; border-radius:8px;
      font-size:14px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:140px; }
  button { background:#1d4ed8; color:#fff; border:0; border-radius:8px;
           padding:11px 22px; font-size:14px; font-weight:700; cursor:pointer; }
  button:hover { background:#1e40af; }
  button.danger { background:#fff; color:#dc2626; border:1px solid #fca5a5;
                  padding:6px 12px; font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:#64748b; font-size:11px; text-transform:uppercase;
       letter-spacing:.5px; padding:8px; border-bottom:2px solid #e2e8f0; }
  td { padding:10px 8px; border-bottom:1px solid #f1f5f9; vertical-align:middle; }
  .pill { display:inline-block; font-weight:800; font-size:12px; padding:3px 12px;
          border-radius:20px; color:#fff; }
  .status { font-size:11px; font-weight:700; letter-spacing:.5px; }
  .links a { text-decoration:none; font-weight:600; margin-right:8px;
             font-size:12px; padding:3px 9px; border-radius:5px; white-space:nowrap; }
  .links.internal a { color:#b45309; background:#fffbeb; border:1px solid #fde68a; }
  .links.internal a:hover { background:#fef3c7; }
  .links.client a { color:#15803d; background:#f0fdf4; border:1px solid #bbf7d0; }
  .links.client a:hover { background:#dcfce7; }
  td.links.internal { border-left:2px solid #fde68a; }
  td.links.client { border-left:2px solid #bbf7d0; }
  .links.guide a { color:#6d28d9; background:#faf5ff; border:1px solid #e9d5ff; }
  .links.guide a:hover { background:#f3e8ff; }
  td.links.guide { border-left:2px solid #e9d5ff; }
  .err { color:#dc2626; font-size:12px; }
  .spin { display:inline-block; width:12px; height:12px; border:2px solid #cbd5e1;
          border-top-color:#1d4ed8; border-radius:50%;
          animation:s .8s linear infinite; vertical-align:-2px; margin-right:5px; }
  @keyframes s { to { transform:rotate(360deg); } }
"""

LOGIN_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEO Audit - Login</title><style>""" + BASE_CSS + """
  .login { max-width:360px; margin:12vh auto; }
</style></head><body>
<div class="login"><div class="card">
  <h2 style="margin-top:0">AI Visibility Audit</h2>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  <form method="post">
    <label>Password</label>
    <input type="password" name="password" autofocus>
    <div style="margin-top:14px"><button type="submit">Sign in</button></div>
  </form>
</div></div></body></html>"""

OAUTH_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize Claude</title><style>""" + BASE_CSS + """
  .box { max-width:380px; margin:11vh auto; }
  .sub { color:#64748b; font-size:13px; margin:0 0 16px; }
</style></head><body>
<div class="box"><div class="card">
  <h2 style="margin:0 0 6px">Authorize Claude</h2>
  <p class="sub">Claude is asking to connect to your AI Visibility Audit
     dashboard. Enter your dashboard password to allow it.</p>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  <form method="post">
    {% for k in ['client_id','redirect_uri','state','code_challenge','code_challenge_method','response_type','scope'] %}
      <input type="hidden" name="{{ k }}" value="{{ args.get(k, '') }}">
    {% endfor %}
    <label>Password</label>
    <input type="password" name="password" autofocus>
    <div style="margin-top:14px"><button type="submit">Allow access</button></div>
  </form>
</div></div></body></html>"""

INDEX_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEO Audit Dashboard</title>
{% if running %}<meta http-equiv="refresh" content="4">{% endif %}
<style>""" + BASE_CSS + """</style></head><body>
<div class="topbar"><div class="wrap">
  <div style="display:flex;align-items:center;gap:22px">
    <h1>AI Visibility Audit</h1>
    <nav class="tabs">
      <a class="on" href="{{ url_for('index') }}">Audits</a>
      <a href="{{ url_for('prospects_page') }}">Prospecting</a>
      <a href="{{ url_for('aitests_page') }}">AI Tests</a>
      <a href="{{ url_for('clients_page') }}">Clients</a>
    </nav>
  </div>
  <a href="{{ url_for('logout') }}">Log out</a>
</div></div>
<div class="wrap">

  <div class="clientbar">
    <a class="cbtn {{ 'on' if cid is none }}" href="{{ url_for('index') }}">All<span class="n">{{ counts['all'] if counts else '' }}</span></a>
    <a class="cbtn {{ 'on' if cid == 0 }}" href="{{ url_for('index', client=0) }}">Unassigned{% if counts %}<span class="n">{{ counts['unassigned'] }}</span>{% endif %}</a>
    {% for c in clients %}
      <a class="cbtn {{ 'on' if cid == c['id'] }}" href="{{ url_for('index', client=c['id']) }}">{{ c['name'] }}<span class="n">{{ c['audit_count'] }}</span></a>
    {% endfor %}
    <a class="cbtn manage" href="{{ url_for('clients_page') }}">+ Manage clients</a>
    <a class="cbtn dlall internal" href="{{ url_for('download_zip', variant='internal', client=cid if cid is not none else None) }}"
       title="ZIP of every Internal report (with fixes) in this view">
       &#8681; Internal ZIP</a>
    <a class="cbtn dlall client" href="{{ url_for('download_zip', variant='client', client=cid if cid is not none else None) }}"
       title="ZIP of every Client report (findings only) in this view - safe to send to clients">
       &#8681; Client ZIP</a>
  </div>
  {% if sel_client %}
    <div class="ctx"><b>{{ sel_client['name'] }}</b>
      &mdash; new audits here are tagged to this client{% if sel_client['brand'] %},
      and reports are branded <b>{{ sel_client['brand'] }}</b>{% endif %}.
      {% if sel_client['notes'] %}<br>{{ sel_client['notes'] }}{% endif %}
    </div>
  {% endif %}

  <div class="card">
    <form method="post" action="{{ url_for('run') }}">
      <div class="row">
        <div style="flex:3">
          <label>Website URL</label>
          <input type="text" name="url" placeholder="clientsite.com" required>
        </div>
        <div style="flex:2">
          <label>Client</label>
          <select name="client_id">
            <option value="">&mdash; Unassigned &mdash;</option>
            {% for c in clients %}
              <option value="{{ c['id'] }}" {{ 'selected' if cid == c['id'] }}>{{ c['name'] }}</option>
            {% endfor %}
          </select>
        </div>
        <div style="flex:2">
          <label>Brand on report</label>
          <input type="text" name="brand" value="{{ default_brand }}">
        </div>
        <div style="flex:0 0 100px">
          <label>Pages</label>
          <input type="number" name="max_pages" value="8" min="2" max="25">
        </div>
      </div>
      <div style="margin-top:12px;display:flex;align-items:center;gap:8px">
        <input type="checkbox" name="deep" value="1" id="deep"
               {{ 'disabled' if not llm_ok }}>
        <label for="deep" style="display:inline;font-weight:400;font-size:13px;color:#475569">
          Deep scan &mdash; adds an AI judge's verdict on citation potential
          (one LLM call, ~10s slower{{ ', requires LLM configuration' if not llm_ok }})
        </label>
      </div>
      <div style="margin-top:14px"><button type="submit">Run Audit</button></div>
    </form>
  </div>

  <div class="card">
    <table>
      <tr><th>Site</th><th>Client</th><th>Date</th><th>Score</th><th>Status</th>
          <th>Internal <span style="text-transform:none;letter-spacing:0">(with fixes)</span></th>
          <th>Client <span style="text-transform:none;letter-spacing:0">(findings only)</span></th>
          <th>Guide <span style="text-transform:none;letter-spacing:0">(sellable)</span></th>
          <th>Off-site</th>
          <th></th></tr>
      {% for r in rows %}
      <tr>
        <td><strong>{{ r['url'].replace('https://','').replace('http://','') }}</strong></td>
        <td>
          <form method="post" action="{{ url_for('audit_assign', audit_id=r['id']) }}" style="margin:0">
            <select name="client_id" onchange="this.form.submit()"
                    style="padding:4px 8px;font-size:12px;border-radius:6px">
              <option value="">&mdash;</option>
              {% for c in clients %}
                <option value="{{ c['id'] }}" {{ 'selected' if r['client_id'] == c['id'] }}>{{ c['name'] }}</option>
              {% endfor %}
            </select>
          </form>
        </td>
        <td>{{ r['created_at'] }}</td>
        <td>
          {% if r['score'] is not none %}
            {% set c = '#16a34a' if r['score']>=90 else '#65a30d' if r['score']>=75
               else '#d97706' if r['score']>=60 else '#ea580c' if r['score']>=40 else '#dc2626' %}
            <span class="pill" style="background:{{ c }}">{{ r['score'] }} &middot; {{ r['grade'] }}</span>
          {% else %}&mdash;{% endif %}
        </td>
        <td class="status">
          {% if r['status']=='running' %}<span class="spin"></span>RUNNING
          {% elif r['status']=='done' %}<span style="color:#16a34a">DONE</span>
          {% else %}<span class="err" title="{{ r['error'] }}">ERROR</span>{% endif %}
          {% if r['status']=='error' and r['error'] %}
            <div class="err" style="font-weight:400">{{ r['error'][:120] }}</div>
          {% endif %}
        </td>
        <td class="links internal">
          {% if r['status']=='done' %}
            <a href="{{ url_for('report', audit_id=r['id']) }}" target="_blank">View</a>
            {% if r['pdf'] %}<a href="{{ url_for('pdf', audit_id=r['id']) }}">PDF</a>{% endif %}
          {% endif %}
        </td>
        <td class="links client">
          {% if r['status']=='done' %}
            <a href="{{ url_for('report', audit_id=r['id'], variant='client') }}" target="_blank">View</a>
            {% if r['pdf'] %}<a href="{{ url_for('pdf', audit_id=r['id'], variant='client') }}">PDF</a>{% endif %}
          {% endif %}
        </td>
        <td class="links guide">
          {% if r['status']=='done' %}
            <a href="{{ url_for('report', audit_id=r['id'], variant='guide') }}" target="_blank">View</a>
            {% if r['pdf'] %}<a href="{{ url_for('pdf', audit_id=r['id'], variant='guide') }}">PDF</a>{% endif %}
          {% endif %}
        </td>
        <td class="links">
          {% if r['status']=='done' %}
            <a href="{{ url_for('manual_page', audit_id=r['id']) }}" style="color:#0f766e;background:#f0fdfa;border:1px solid #99f6e4">Manual</a>
          {% endif %}
        </td>
        <td style="text-align:right">
          <form method="post" action="{{ url_for('delete', audit_id=r['id']) }}"
                onsubmit="return confirm('Delete this audit?')" style="margin:0">
            <button class="danger" type="submit">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
      {% if not rows %}<tr><td colspan="10" style="color:#94a3b8">
        No audits yet. Enter a URL above and click Run Audit.</td></tr>{% endif %}
    </table>
  </div>
</div></body></html>"""


PROSPECTS_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prospecting - GEO Audit</title>
{% if running %}<meta http-equiv="refresh" content="6">{% endif %}
<style>""" + BASE_CSS + """
  .cfg { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:16px; }
  .cfg span { font-size:11px; font-weight:700; padding:4px 11px; border-radius:20px; }
  .ok  { background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; }
  .off { background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; }
  .st { font-size:10.5px; font-weight:800; letter-spacing:.5px; padding:2px 9px;
        border-radius:20px; white-space:nowrap; }
  .st-found    { background:#eff6ff; color:#1d4ed8; }
  .st-auditing { background:#fffbeb; color:#b45309; }
  .st-audited  { background:#f0fdf4; color:#15803d; }
  .st-drafted  { background:#faf5ff; color:#7e22ce; }
  .st-sent     { background:#ecfdf5; color:#047857; }
  .st-error    { background:#fef2f2; color:#b91c1c; }
  .hint { font-size:12px; color:#64748b; margin-top:8px; }
  .autosend { display:flex; align-items:center; gap:8px; font-size:13px;
              color:#475569; margin-top:12px; }
</style></head><body>
<div class="topbar"><div class="wrap">
  <div style="display:flex;align-items:center;gap:22px">
    <h1>AI Visibility Audit</h1>
    <nav class="tabs">
      <a href="{{ url_for('index') }}">Audits</a>
      <a class="on" href="{{ url_for('prospects_page') }}">Prospecting</a>
      <a href="{{ url_for('aitests_page') }}">AI Tests</a>
      <a href="{{ url_for('clients_page') }}">Clients</a>
    </nav>
  </div>
  <a href="{{ url_for('logout') }}">Log out</a>
</div></div>
<div class="wrap">

  <div class="clientbar">
    <a class="cbtn {{ 'on' if cid is none }}" href="{{ url_for('prospects_page') }}">All</a>
    <a class="cbtn {{ 'on' if cid == 0 }}" href="{{ url_for('prospects_page', client=0) }}">Unassigned</a>
    {% for c in clients %}
      <a class="cbtn {{ 'on' if cid == c['id'] }}" href="{{ url_for('prospects_page', client=c['id']) }}">{{ c['name'] }}</a>
    {% endfor %}
    <a class="cbtn manage" href="{{ url_for('clients_page') }}">+ Manage clients</a>
  </div>
  {% if sel_client %}
    <div class="ctx">Prospects here belong to <b>{{ sel_client['name'] }}</b>.
      Duplicate businesses are only blocked within this client's list.</div>
  {% endif %}

  <div class="cfg">
    <span class="{{ 'ok' if places_ok else 'off' }}">Google Places {{ 'connected' if places_ok else 'not configured' }}</span>
    <span class="{{ 'ok' if llm_ok else 'off' }}">Email AI {{ 'connected' if llm_ok else 'not configured' }}</span>
    <span class="{{ 'ok' if smtp_ok else 'off' }}">SMTP {{ 'connected' if smtp_ok else 'not configured' }}</span>
    <span class="{{ 'ok' if auto_send_master else 'off' }}">Auto-send {{ 'ENABLED (cap ' ~ cap ~ '/day, ' ~ sent_today ~ ' sent today)' if auto_send_master else 'off (drafts only)' }}</span>
  </div>

  <div class="card">
    <form method="post" action="{{ url_for('prospects_search') }}">
      <input type="hidden" name="client_id" value="{{ cid if cid else '' }}">
      <div class="row">
        <div style="flex:3">
          <label>Find businesses (Google Places search)</label>
          <input type="text" name="query" placeholder="e.g. personal injury lawyers in Denver, CO" required {{ 'disabled' if not places_ok }}>
        </div>
        <div style="flex:0 0 110px">
          <label>Max results</label>
          <input type="number" name="max_results" value="20" min="1" max="60" {{ 'disabled' if not places_ok }}>
        </div>
      </div>
      <div style="margin-top:14px">
        <button type="submit" {{ 'disabled' if not places_ok }}>Find Businesses</button>
      </div>
      {% if not places_ok %}<div class="hint">Set GOOGLE_PLACES_API_KEY in Coolify environment variables to enable search.</div>{% endif %}
    </form>
  </div>

  <div class="card">
    <form method="post" action="{{ url_for('prospects_process') }}">
      <input type="hidden" name="client_id" value="{{ cid if cid else '' }}">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
        <div>
          <button type="submit">Audit &amp; Draft Selected</button>
          <span class="hint">No selection = process all "found" (max 25). Each prospect: audit &rarr; find contact email &rarr; AI drafts outreach.</span>
        </div>
        <div style="flex:0 0 110px">
          <label>Pages/site</label>
          <input type="number" name="max_pages" value="6" min="2" max="15">
        </div>
      </div>
      <div class="autosend">
        <input type="checkbox" name="auto_send" value="1" id="as"
               {{ 'disabled' if not (auto_send_master and smtp_ok) }}>
        <label for="as" style="display:inline;font-weight:400">
          Auto-send drafts after generation
          {% if not auto_send_master %} (disabled &mdash; set AUTO_SEND=1 to allow){% elif not smtp_ok %} (SMTP not configured){% endif %}
        </label>
      </div>

      <table style="margin-top:14px">
        <tr><th></th><th>Business</th><th>Website</th><th>Score</th><th>Contact</th><th>Status</th><th>Email</th><th></th></tr>
        {% for r in rows %}
        <tr>
          <td><input type="checkbox" name="ids" value="{{ r['id'] }}"
                     {{ 'disabled' if r['status'] not in ('found','error') }}></td>
          <td><strong>{{ r['name'] }}</strong>
              <div style="color:#94a3b8;font-size:11px">{{ (r['address'] or '')[:48] }}</div></td>
          <td><a href="{{ r['website'] }}" target="_blank" style="color:#1d4ed8">{{ r['domain'] }}</a></td>
          <td>
            {% if r['score'] is not none %}
              {% set c = '#16a34a' if r['score']>=90 else '#65a30d' if r['score']>=75
                 else '#d97706' if r['score']>=60 else '#ea580c' if r['score']>=40 else '#dc2626' %}
              <span class="pill" style="background:{{ c }}">{{ r['score'] }}</span>
              {% if r['audit_id'] %}<a href="{{ url_for('report', audit_id=r['audit_id']) }}" target="_blank" style="font-size:11px;margin-left:5px">report</a>{% endif %}
            {% else %}&mdash;{% endif %}
          </td>
          <td style="font-size:11.5px">{{ r['email'] or '&mdash;'|safe }}</td>
          <td>
            <span class="st st-{{ r['status'] }}">{{ r['status']|upper }}</span>
            {% if r['error'] %}<div class="err" style="font-size:10.5px;font-weight:400">{{ r['error'][:90] }}</div>{% endif %}
          </td>
          <td>
            {% if r['email_subject'] %}
              <a href="{{ url_for('prospect_email', pid=r['id']) }}" style="color:#7e22ce;font-weight:700;font-size:12px">
                {{ 'View sent' if r['status']=='sent' else 'Edit draft' }}</a>
            {% endif %}
          </td>
          <td style="text-align:right">
            <button class="danger" formmethod="post"
              formaction="{{ url_for('prospect_delete', pid=r['id']) }}"
              onclick="return confirm('Remove this prospect?')">×</button>
          </td>
        </tr>
        {% endfor %}
        {% if not rows %}<tr><td colspan="8" style="color:#94a3b8">
          No prospects yet. Search for businesses above.</td></tr>{% endif %}
      </table>
    </form>
  </div>
</div></body></html>"""


EMAIL_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outreach draft - {{ p['name'] }}</title>
<style>""" + BASE_CSS + """
  textarea { width:100%; min-height:280px; padding:12px; border:1px solid #cbd5e1;
             border-radius:8px; font-size:14px; font-family:inherit; line-height:1.6; }
  .meta { color:#64748b; font-size:13px; margin-bottom:14px; }
  .msg { background:#f0fdf4; border:1px solid #bbf7d0; color:#15803d;
         border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
  .footerprev { background:#f8fafc; border:1px dashed #cbd5e1; border-radius:8px;
                padding:10px 14px; color:#64748b; font-size:12px;
                white-space:pre-line; margin-top:10px; }
  button.secondary { background:#fff; color:#1d4ed8; border:1px solid #93c5fd; }
</style></head><body>
<div class="topbar"><div class="wrap">
  <h1>Outreach &mdash; {{ p['name'] }}</h1>
  <a href="{{ url_for('prospects_page') }}">&larr; Back to prospecting</a>
</div></div>
<div class="wrap">
  {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
  <div class="card">
    <div class="meta">
      {{ p['domain'] }} &nbsp;&bull;&nbsp; Score {{ p['score'] }}/100 ({{ p['grade'] }})
      &nbsp;&bull;&nbsp; Status: {{ p['status']|upper }}
      {% if p['sent_at'] %}&nbsp;&bull;&nbsp; Sent {{ p['sent_at'] }}{% endif %}
    </div>
    <form method="post">
      <label>To</label>
      <input type="text" name="to" value="{{ p['email'] or '' }}"
             placeholder="No email found on their site - paste one here">
      <div style="height:12px"></div>
      <label>Subject</label>
      <input type="text" name="subject" value="{{ p['email_subject'] or '' }}">
      <div style="height:12px"></div>
      <label>Body</label>
      <textarea name="body">{{ p['email_body'] or '' }}</textarea>
      <div class="footerprev">This footer is appended automatically on send:{{ footer }}</div>
      <div style="margin-top:16px;display:flex;gap:10px">
        <button type="submit" name="action" value="save" class="secondary">Save draft</button>
        <button type="submit" name="action" value="send"
                {{ 'disabled' if not smtp_ok }}
                onclick="return confirm('Send this email now?')">Send email</button>
        {% if not smtp_ok %}<span style="color:#94a3b8;font-size:12px;align-self:center">SMTP not configured</span>{% endif %}
      </div>
    </form>
  </div>
</div></body></html>"""


CLIENTS_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clients - GEO Audit</title>
<style>""" + BASE_CSS + """
  .cl { border:1px solid #e2e8f0; border-radius:10px; padding:14px 16px;
        margin-bottom:10px; }
  .cl-head { display:flex; justify-content:space-between; align-items:center;
             margin-bottom:8px; }
  .cl-name { font-size:16px; font-weight:700; }
  .cl-meta { color:#64748b; font-size:12px; }
  .msg { background:#f0fdf4; border:1px solid #bbf7d0; color:#15803d;
         border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
  details summary { cursor:pointer; color:#1d4ed8; font-size:12px;
                    font-weight:600; margin-top:6px; }
</style></head><body>
<div class="topbar"><div class="wrap">
  <div style="display:flex;align-items:center;gap:22px">
    <h1>AI Visibility Audit</h1>
    <nav class="tabs">
      <a href="{{ url_for('index') }}">Audits</a>
      <a href="{{ url_for('prospects_page') }}">Prospecting</a>
      <a href="{{ url_for('aitests_page') }}">AI Tests</a>
      <a class="on" href="{{ url_for('clients_page') }}">Clients</a>
    </nav>
  </div>
  <a href="{{ url_for('logout') }}">Log out</a>
</div></div>
<div class="wrap">
  {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}

  <div class="card">
    <form method="post">
      <div class="row">
        <div style="flex:2">
          <label>Client name</label>
          <input type="text" name="name" placeholder="e.g. Bosseo, or Weber Law" required>
        </div>
        <div style="flex:2">
          <label>Report brand <span style="font-weight:400;color:#94a3b8">(optional &mdash; white-label)</span></label>
          <input type="text" name="brand" placeholder="{{ default_brand }}">
        </div>
      </div>
      <div style="margin-top:10px">
        <label>Notes <span style="font-weight:400;color:#94a3b8">(optional)</span></label>
        <input type="text" name="notes" placeholder="Anything you want to remember about this client">
      </div>
      <div style="margin-top:14px"><button type="submit">Add Client</button></div>
    </form>
  </div>

  <div class="card">
    {% for c in clients %}
    <div class="cl">
      <div class="cl-head">
        <div>
          <div class="cl-name">{{ c['name'] }}</div>
          <div class="cl-meta">
            {{ c['audit_count'] }} audit{{ '' if c['audit_count']==1 else 's' }}
            {% if c['brand'] %}&nbsp;&bull;&nbsp; reports branded &ldquo;{{ c['brand'] }}&rdquo;{% endif %}
            {% if c['notes'] %}<br>{{ c['notes'] }}{% endif %}
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <a href="{{ url_for('index', client=c['id']) }}"
             style="color:#1d4ed8;text-decoration:none;font-weight:600;font-size:13px">Open &rarr;</a>
          <form method="post" action="{{ url_for('client_delete', cid=c['id']) }}" style="margin:0"
                onsubmit="return confirm('Delete this client? Their audits are KEPT and become Unassigned.')">
            <button class="danger" type="submit">Delete</button>
          </form>
        </div>
      </div>
      <details>
        <summary>Edit</summary>
        <form method="post" action="{{ url_for('client_edit', cid=c['id']) }}" style="margin-top:8px">
          <div class="row">
            <div><label>Name</label><input type="text" name="name" value="{{ c['name'] }}"></div>
            <div><label>Report brand</label><input type="text" name="brand" value="{{ c['brand'] or '' }}"></div>
          </div>
          <div style="margin-top:8px">
            <label>Notes</label>
            <input type="text" name="notes" value="{{ c['notes'] or '' }}">
          </div>
          <div style="margin-top:10px"><button type="submit">Save</button></div>
        </form>
      </details>
    </div>
    {% endfor %}
    {% if not clients %}
      <div style="color:#94a3b8;font-size:13px">
        No clients yet. Add one above &mdash; then audits and prospects can be filed
        under it and kept separate from everything else.
      </div>
    {% endif %}
  </div>
</div></body></html>"""


AITESTS_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Tests - GEO Audit</title>
<style>""" + BASE_CSS + """
  .scorecard { display:flex; gap:20px; align-items:center; flex-wrap:wrap; }
  .bignum { font-size:44px; font-weight:800; }
  .ebar { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
  .ebar .en { flex:0 0 150px; font-size:12px; font-weight:600; color:#475569; }
  .ebar .tr { flex:1; height:7px; background:#f1f5f9; border-radius:4px; overflow:hidden; }
  .ebar .tr div { height:100%; border-radius:4px; }
  .ebar .pv { flex:0 0 68px; font-size:12px; font-weight:700; text-align:right; }
  .res { font-size:10.5px; font-weight:800; letter-spacing:.4px; padding:2px 9px;
         border-radius:20px; white-space:nowrap; }
  .r3 { background:#f0fdf4; color:#15803d; } .r2 { background:#eff6ff; color:#1d4ed8; }
  .r1 { background:#fffbeb; color:#b45309; } .r0 { background:#fef2f2; color:#b91c1c; }
  .pk { background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;
        padding:9px 12px; margin-bottom:7px; display:flex; gap:10px;
        align-items:center; justify-content:space-between; }
  .pk code { font-size:12.5px; color:#0f172a; }
  details { border:1px solid #e2e8f0; border-radius:9px; padding:12px 16px;
            margin-bottom:9px; }
  details summary { cursor:pointer; font-weight:700; color:#0f172a; font-size:13.5px; }
  details ol { margin:10px 0 4px; padding-left:20px; color:#334155; font-size:13px; }
  details li { margin-bottom:6px; }
  details .warn { background:#fffbeb; border:1px solid #fde68a; border-radius:7px;
                  padding:8px 12px; color:#92400e; font-size:12.5px; margin-top:8px; }
  .rubric td { font-size:12.5px; }
</style></head><body>
<div class="topbar"><div class="wrap">
  <div style="display:flex;align-items:center;gap:22px">
    <h1>AI Visibility Audit</h1>
    <nav class="tabs">
      <a href="{{ url_for('index') }}">Audits</a>
      <a href="{{ url_for('prospects_page') }}">Prospecting</a>
      <a class="on" href="{{ url_for('aitests_page') }}">AI Tests</a>
      <a href="{{ url_for('clients_page') }}">Clients</a>
    </nav>
  </div>
  <a href="{{ url_for('logout') }}">Log out</a>
</div></div>
<div class="wrap">

  <div class="clientbar">
    <a class="cbtn {{ 'on' if cid is none }}" href="{{ url_for('aitests_page') }}">All</a>
    <a class="cbtn {{ 'on' if cid == 0 }}" href="{{ url_for('aitests_page', client=0) }}">Unassigned</a>
    {% for c in clients %}
      <a class="cbtn {{ 'on' if cid == c['id'] }}" href="{{ url_for('aitests_page', client=c['id']) }}">{{ c['name'] }}</a>
    {% endfor %}
    <a class="cbtn manage" href="{{ url_for('clients_page') }}">+ Manage clients</a>
  </div>
  {% if sel_client %}
    <div class="ctx">Recording AI presence tests for <b>{{ sel_client['name'] }}</b>.
      Re-testing the same prompt later replaces its result in the score; old
      entries are kept as history.</div>
  {% endif %}

  {% if scores %}
  <div class="card">
    <div class="scorecard">
      <div style="text-align:center">
        {% set pc = scores['pct'] %}
        <div class="bignum" style="color:{{ '#16a34a' if pc>=70 else '#d97706' if pc>=40 else '#dc2626' }}">{{ pc }}%</div>
        <div style="font-size:11px;color:#94a3b8;letter-spacing:.6px">AI PRESENCE SCORE<br>{{ scores['tests'] }} active tests</div>
      </div>
      <div style="flex:1;min-width:260px">
        {% for ek, ev in scores['per_engine'].items() %}
        <div class="ebar">
          <div class="en">{{ engines.get(ek, ek) }}</div>
          <div class="tr"><div style="width:{{ ev['pct'] }}%;background:{{ '#16a34a' if ev['pct']>=70 else '#d97706' if ev['pct']>=40 else '#dc2626' }}"></div></div>
          <div class="pv">{{ ev['pct'] }}% <span style="color:#94a3b8;font-weight:400">({{ ev['n'] }})</span></div>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>
  {% endif %}

  <div class="card">
    <h3 style="margin:0 0 4px">1 &middot; Generate the standard prompt pack</h3>
    <p style="color:#64748b;font-size:13px;margin:0 0 12px">Always test the same
      prompts, phrased the same way - that's what makes this month's score
      comparable to next month's.</p>
    <form method="get">
      {% if cid is not none %}<input type="hidden" name="client" value="{{ cid }}">{% endif %}
      <div class="row">
        <div><label>Service type</label><input type="text" name="svc" value="{{ svc }}" placeholder="personal injury lawyer"></div>
        <div><label>City</label><input type="text" name="city" value="{{ city }}" placeholder="Denver, CO"></div>
        <div><label>Business name <span style="font-weight:400;color:#94a3b8">(optional)</span></label>
             <input type="text" name="biz" value="{{ biz }}" placeholder="Smith Legal"></div>
      </div>
      <div style="margin-top:12px"><button type="submit">Generate prompts</button></div>
    </form>
    {% if pack %}
      <div style="margin-top:14px">
      {% for p in pack %}<div class="pk"><code>{{ p }}</code></div>{% endfor %}
      </div>
      <p style="color:#64748b;font-size:12.5px;margin:8px 0 0">Copy each prompt into
        each engine (guides below), then record what happened in step 2.</p>
    {% endif %}
  </div>

  <div class="card">
    <h3 style="margin:0 0 12px">2 &middot; Record a result</h3>
    <form method="post" action="{{ url_for('aitests_add') }}">
      <input type="hidden" name="client_id" value="{{ cid if cid else '' }}">
      <div class="row">
        <div style="flex:0 0 190px">
          <label>Engine</label>
          <select name="engine">{% for k, v in engines.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
        </div>
        <div style="flex:3"><label>Prompt (exactly as asked)</label>
          <input type="text" name="prompt" required placeholder="best personal injury lawyer in Denver"></div>
        <div style="flex:0 0 250px">
          <label>Result</label>
          <select name="result">{% for k, v in labels.items() %}<option value="{{ k }}">{{ k }} - {{ v }}</option>{% endfor %}</select>
        </div>
      </div>
      <div style="margin-top:10px"><label>Notes <span style="font-weight:400;color:#94a3b8">(who WAS recommended, position, wording)</span></label>
        <input type="text" name="notes" placeholder="Recommended Jones Law and Miller & Co; client absent"></div>
      <div style="margin-top:12px"><button type="submit">Save result</button></div>
    </form>

    <table style="margin-top:16px">
      <tr><th>Date</th><th>Engine</th><th>Prompt</th><th>Result</th><th>Notes</th><th></th></tr>
      {% for r in rows %}
      <tr>
        <td style="white-space:nowrap;color:#94a3b8;font-size:12px">{{ r['tested_at'] }}</td>
        <td style="font-weight:600;font-size:12.5px">{{ engines.get(r['engine'], r['engine']) }}</td>
        <td style="font-size:12.5px">{{ r['prompt'] }}</td>
        <td><span class="res r{{ r['result'] }}">{{ r['result'] }} &middot; {{ labels[r['result']] }}</span></td>
        <td style="color:#64748b;font-size:12px">{{ r['notes'] or '' }}</td>
        <td style="text-align:right">
          <form method="post" action="{{ url_for('aitests_delete', tid=r['id']) }}" style="margin:0">
            <button class="danger" onclick="return confirm('Delete this entry?')">×</button>
          </form>
        </td>
      </tr>
      {% endfor %}
      {% if not rows %}<tr><td colspan="6" style="color:#94a3b8">No tests recorded yet.
        Generate a prompt pack above, run the prompts in each engine, and record what happened.</td></tr>{% endif %}
    </table>
  </div>

  <div class="card">
    <h3 style="margin:0 0 4px">How to test - step by step per engine</h3>
    <p style="color:#64748b;font-size:13px;margin:0 0 12px">Consistency is the whole
      game: fresh session, exact prompt, record immediately. If the method drifts,
      the score stops meaning anything.</p>

    <details><summary>Scoring rubric - what number to record</summary>
      <table class="rubric" style="margin-top:10px">
        <tr><td><span class="res r3">3</span></td><td><b>Recommended by name</b> - the engine names the business as a recommendation or top answer in its own words.</td></tr>
        <tr><td><span class="res r2">2</span></td><td><b>Mentioned / cited</b> - the business appears in the answer among others, or its site is cited as a source for a claim.</td></tr>
        <tr><td><span class="res r1">1</span></td><td><b>Sources only</b> - not in the answer text, but the site shows in the sources/links panel, or appears only after you ask a follow-up.</td></tr>
        <tr><td><span class="res r0">0</span></td><td><b>Not present</b> - absent entirely, or only competitors are named.</td></tr>
      </table>
      <div class="warn">Score what the engine actually said, not what you hoped. A
        generous score today makes next month's improvement invisible.</div>
    </details>

    <details><summary>ChatGPT</summary>
      <ol>
        <li>Log out, or open a Temporary Chat (model picker &rarr; Temporary) so
            memory and custom instructions don't personalise the answer.</li>
        <li>Make sure Search is enabled (the globe icon) - visibility testing is
            about the search-augmented answer, not training data alone.</li>
        <li>Paste the prompt exactly as written in the pack. No extra context.</li>
        <li>Read the full answer AND expand the sources. Score with the rubric.</li>
        <li>Record the result immediately, noting who WAS recommended.</li>
        <li>New Temporary Chat for the next prompt - never reuse a conversation,
            earlier answers contaminate later ones.</li>
      </ol>
    </details>

    <details><summary>Google AI Overviews &amp; AI Mode</summary>
      <ol>
        <li>Open an Incognito window (personalised results otherwise).</li>
        <li>Location matters here more than any other engine: if testing for a
            city you're not in, append the city to the query and note it.</li>
        <li>Search the prompt. If an AI Overview appears, score it. If none
            appears, note "no AI Overview shown" - that itself is a finding.</li>
        <li>For AI Mode, switch to the AI Mode tab and ask the same prompt.</li>
        <li>Record AI Overviews and AI Mode as separate entries - they behave
            differently.</li>
      </ol>
      <div class="warn">You're testing from Bangladesh for US businesses:
        results are location-influenced, so treat Google scores as directional.
        Where possible, have the client run the same searches locally and send
        screenshots - their view is the ground truth.</div>
    </details>

    <details><summary>Perplexity</summary>
      <ol>
        <li>Log out or use a private window.</li>
        <li>Paste the prompt. Perplexity always searches, so no toggle needed.</li>
        <li>Check both the answer text and the numbered citations - a citation
            of the client's site scores 2 even if the name isn't in the prose.</li>
        <li>Note the citation position (source #1 vs #8) in the notes field.</li>
      </ol>
    </details>

    <details><summary>Gemini</summary>
      <ol>
        <li>Use a profile with no prior history about the client, or a private
            window.</li>
        <li>Paste the prompt exactly. Check any "Sources" or search suggestions
            it shows alongside the answer.</li>
        <li>Gemini often hedges with "consult local directories" - if it names
            no businesses at all, score 0 and note "named nobody" (that's
            different from naming competitors).</li>
      </ol>
    </details>

    <details><summary>Claude</summary>
      <ol>
        <li>Open a fresh chat with web search enabled.</li>
        <li>Paste the prompt exactly; score answer text and cited sources with
            the same rubric.</li>
      </ol>
    </details>

    <details><summary>Bing Copilot</summary>
      <ol>
        <li>Private window, copilot.microsoft.com.</li>
        <li>Paste the prompt; check answer and the link cards beneath it.</li>
      </ol>
    </details>

    <details><summary>Method rules (read once, follow always)</summary>
      <ol>
        <li><b>Same prompts, same wording, every round.</b> The pack generator
            exists so the phrasing never drifts.</li>
        <li><b>Test monthly</b>, same week each month. AI answers move slowly;
            more frequent testing measures noise.</li>
        <li><b>One engine, one prompt, one entry.</b> Re-testing later just adds
            a new entry - the score automatically uses the newest.</li>
        <li><b>Screenshot everything.</b> The before/after screenshots are your
            proof of progress when you report to the client.</li>
        <li><b>Log competitors in notes.</b> Who the engine DID recommend is
            next month's gap analysis for free.</li>
      </ol>
    </details>
  </div>
</div></body></html>"""


MANUAL_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Manual checks - {{ site }}</title>
<style>""" + BASE_CSS + """
  .mi { border:1px solid #e2e8f0; border-radius:9px; padding:14px 16px; margin-bottom:11px; }
  .mi-head { display:flex; align-items:center; gap:9px; margin-bottom:5px; }
  .mi-head h3 { margin:0; font-size:14px; }
  .impact { font-size:8.5px; font-weight:800; letter-spacing:.8px; padding:2px 7px;
            border-radius:20px; border:1px solid; white-space:nowrap; }
  .why { color:#64748b; font-size:12.5px; margin-bottom:8px; }
  details { margin-bottom:9px; }
  details summary { cursor:pointer; color:#1d4ed8; font-size:12px; font-weight:700; }
  details ol { margin:8px 0 2px; padding-left:20px; font-size:12.5px; color:#334155; }
  details li { margin-bottom:4px; }
  .msg { background:#f0fdf4; border:1px solid #bbf7d0; color:#15803d;
         border-radius:8px; padding:10px 14px; margin-bottom:14px; font-size:13px; }
  .mrow { display:flex; gap:10px; }
  .mrow select { flex:0 0 210px; }
  .mrow input { flex:1; }
</style></head><body>
<div class="topbar"><div class="wrap">
  <h1>Off-site checks &mdash; {{ site }}</h1>
  <a href="{{ url_for('index') }}">&larr; Back to audits</a>
</div></div>
<div class="wrap">
  {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
  <div class="ctx">These are the topics no crawler can verify - profiles, reviews,
    mentions. Follow the steps, pick a verdict, note what you found. Saving
    <b>regenerates all three reports</b>: results appear in the internal and client
    copies{% if e_max %} (extended score currently <b>{{ e_earn }}/{{ e_max }}</b>){% endif %};
    anything left unverified shows in the client copy as a locked
    &ldquo;full audit&rdquo; item &mdash; your upsell.</div>
  <form method="post">
    {% for m in items %}
    {% set imp = {'critical':('#dc2626','#fef2f2'),'high':('#ea580c','#fff7ed'),
                  'medium':('#d97706','#fffbeb'),'low':('#64748b','#f8fafc')}[m['impact']] %}
    <div class="mi">
      <div class="mi-head">
        <span class="impact" style="color:{{ imp[0] }};background:{{ imp[1] }};border-color:{{ imp[0] }}40">{{ m['impact']|upper }}</span>
        <h3>{{ m['name'] }}</h3>
        <span style="color:#94a3b8;font-size:12px;margin-left:auto">{{ m['max_points'] }} pts</span>
      </div>
      <div class="why">{{ m['why'] }}</div>
      <details><summary>How to verify - step by step</summary>
        <ol>{% for s in m['how'] %}<li>{{ s|safe }}</li>{% endfor %}</ol>
      </details>
      <div class="mrow">
        <select name="status-{{ m['key'] }}">
          <option value="">&mdash; not verified &mdash;</option>
          <option value="pass" {{ 'selected' if m['status']=='pass' }}>Pass</option>
          <option value="warn" {{ 'selected' if m['status']=='warn' }}>Needs work</option>
          <option value="fail" {{ 'selected' if m['status']=='fail' }}>Fail</option>
          {% if m['status'] %}<option value="clear">Clear this result</option>{% endif %}
        </select>
        <input type="text" name="notes-{{ m['key'] }}" value="{{ m['notes'] }}"
               placeholder="What you found (goes into the report)">
      </div>
    </div>
    {% endfor %}
    <button type="submit">Save &amp; regenerate reports</button>
  </form>
</div></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("GEO_PORT", 8080)))
