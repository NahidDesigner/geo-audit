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

import ipaddress
import os
import secrets
import socket
import sqlite3
import threading
import traceback
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, redirect, render_template_string,
                   request, send_file, session, url_for)

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prospect_domain ON prospects(domain)")


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


# ----------------------------------------------------------------------------
# Audit runner (background thread)
# ----------------------------------------------------------------------------

def run_audit_job(audit_id, url, brand, max_pages):
    out_base = os.path.join(REPORT_DIR, str(audit_id))
    try:
        result = run_audit(url, brand, max_pages, out_base)
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
    with db() as conn:
        rows = conn.execute("SELECT * FROM audits ORDER BY id DESC LIMIT 200").fetchall()
    running = any(r["status"] == "running" for r in rows)
    return render_template_string(INDEX_TMPL, rows=rows, running=running,
                                  default_brand=DEFAULT_BRAND)


@app.route("/run", methods=["POST"])
@login_required
def run():
    url = (request.form.get("url") or "").strip()
    brand = (request.form.get("brand") or DEFAULT_BRAND).strip() or DEFAULT_BRAND
    try:
        max_pages = max(2, min(int(request.form.get("max_pages", 6)), 25))
    except ValueError:
        max_pages = 6
    if not url:
        return redirect(url_for("index"))
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not host_is_public(url):
        with db() as conn:
            conn.execute("INSERT INTO audits (url,brand,max_pages,status,error,created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (url, brand, max_pages, "error",
                          "Host is not a public website (or DNS failed).",
                          datetime.now().strftime("%Y-%m-%d %H:%M")))
        return redirect(url_for("index"))

    with _db_lock, db() as conn:
        cur = conn.execute(
            "INSERT INTO audits (url,brand,max_pages,status,created_at) VALUES (?,?,?,?,?)",
            (url, brand, max_pages, "running", datetime.now().strftime("%Y-%m-%d %H:%M")))
        audit_id = cur.lastrowid
    threading.Thread(target=run_audit_job, args=(audit_id, url, brand, max_pages),
                     daemon=True).start()
    return redirect(url_for("index"))


def _report_path(audit_id, ext, client=False):
    suffix = "-client" if client else ""
    p = os.path.join(REPORT_DIR, f"{audit_id}{suffix}.{ext}")
    return p if os.path.exists(p) else None


@app.route("/report/<int:audit_id>")
@app.route("/report/<int:audit_id>/<variant>")
@login_required
def report(audit_id, variant="internal"):
    p = _report_path(audit_id, "html", client=(variant == "client"))
    if not p:
        abort(404)
    with open(p, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.route("/pdf/<int:audit_id>")
@app.route("/pdf/<int:audit_id>/<variant>")
@login_required
def pdf(audit_id, variant="internal"):
    is_client = variant == "client"
    p = _report_path(audit_id, "pdf", client=is_client)
    if not p:
        abort(404)
    with db() as conn:
        row = conn.execute("SELECT url FROM audits WHERE id=?", (audit_id,)).fetchone()
    site = urlparse(row["url"]).netloc.replace(":", "_") if row else "site"
    tag = "" if is_client else "-INTERNAL"
    return send_file(p, as_attachment=True,
                     download_name=f"ai-visibility-audit-{site}{tag}.pdf")


@app.route("/delete/<int:audit_id>", methods=["POST"])
@login_required
def delete(audit_id):
    for ext in ("html", "pdf", "json"):
        for client in (False, True):
            p = _report_path(audit_id, ext, client=client)
            if p:
                os.remove(p)
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM audits WHERE id=?", (audit_id,))
    return redirect(url_for("index"))


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
                "INSERT INTO audits (url,brand,max_pages,status,created_at) "
                "VALUES (?,?,?,?,?)",
                (p["website"], brand, max_pages, "running",
                 datetime.now().strftime("%Y-%m-%d %H:%M")))
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
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM prospects ORDER BY id DESC LIMIT 300").fetchall()
    busy = any(r["status"] in ("auditing", "found") and r["status"] == "auditing"
               for r in rows)
    running = any(r["status"] == "auditing" for r in rows)
    return render_template_string(
        PROSPECTS_TMPL, rows=rows, running=running,
        places_ok=PLACES_OK, llm_ok=LLM_OK, smtp_ok=SMTP_OK,
        auto_send_master=AUTO_SEND_MASTER, cap=SEND_DAILY_CAP,
        sent_today=_sent_today(), default_brand=DEFAULT_BRAND)


@app.route("/prospects/search", methods=["POST"])
@login_required
def prospects_search():
    query = (request.form.get("query") or "").strip()
    if not query or not PLACES_OK:
        return redirect(url_for("prospects_page"))
    try:
        max_r = max(1, min(int(request.form.get("max_results", 20)), 60))
    except ValueError:
        max_r = 20
    try:
        results = P.search_places(query, PLACES_KEY, max_r)
    except Exception as e:
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO prospects (name,website,domain,status,error,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"Search failed: {query}", "-", "-", "error", str(e)[:300],
                 datetime.now().strftime("%Y-%m-%d %H:%M")))
        return redirect(url_for("prospects_page"))

    added = 0
    with _db_lock, db() as conn:
        for r in results:
            domain = urlparse(r["website"]).netloc.lower().removeprefix("www.")
            if not domain:
                continue
            dup = conn.execute("SELECT 1 FROM prospects WHERE domain=?",
                               (domain,)).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO prospects (place_id,name,website,domain,address,"
                "status,created_at) VALUES (?,?,?,?,?,?,?)",
                (r["place_id"], r["name"], r["website"], domain, r["address"],
                 "found", datetime.now().strftime("%Y-%m-%d %H:%M")))
            added += 1
    return redirect(url_for("prospects_page"))


@app.route("/prospects/process", methods=["POST"])
@login_required
def prospects_process():
    ids = [int(i) for i in request.form.getlist("ids")]
    auto_send = request.form.get("auto_send") == "1"
    try:
        max_pages = max(2, min(int(request.form.get("max_pages", 6)), 15))
    except ValueError:
        max_pages = 6
    if not ids:
        with db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM prospects WHERE status='found' ORDER BY id"
            ).fetchall()][:25]
    threading.Thread(target=process_batch,
                     args=(ids, DEFAULT_BRAND, max_pages, auto_send),
                     daemon=True).start()
    return redirect(url_for("prospects_page"))


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
    </nav>
  </div>
  <a href="{{ url_for('logout') }}">Log out</a>
</div></div>
<div class="wrap">
  <div class="card">
    <form method="post" action="{{ url_for('run') }}">
      <div class="row">
        <div style="flex:3">
          <label>Website URL</label>
          <input type="text" name="url" placeholder="clientsite.com" required>
        </div>
        <div style="flex:2">
          <label>Brand on report</label>
          <input type="text" name="brand" value="{{ default_brand }}">
        </div>
        <div style="flex:0 0 110px">
          <label>Pages</label>
          <input type="number" name="max_pages" value="8" min="2" max="25">
        </div>
      </div>
      <div style="margin-top:14px"><button type="submit">Run Audit</button></div>
    </form>
  </div>

  <div class="card">
    <table>
      <tr><th>Site</th><th>Date</th><th>Score</th><th>Status</th>
          <th>Internal <span style="text-transform:none;letter-spacing:0">(with fixes)</span></th>
          <th>Client <span style="text-transform:none;letter-spacing:0">(findings only)</span></th>
          <th></th></tr>
      {% for r in rows %}
      <tr>
        <td><strong>{{ r['url'].replace('https://','').replace('http://','') }}</strong></td>
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
        <td style="text-align:right">
          <form method="post" action="{{ url_for('delete', audit_id=r['id']) }}"
                onsubmit="return confirm('Delete this audit?')" style="margin:0">
            <button class="danger" type="submit">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
      {% if not rows %}<tr><td colspan="7" style="color:#94a3b8">
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
    </nav>
  </div>
  <a href="{{ url_for('logout') }}">Log out</a>
</div></div>
<div class="wrap">

  <div class="cfg">
    <span class="{{ 'ok' if places_ok else 'off' }}">Google Places {{ 'connected' if places_ok else 'not configured' }}</span>
    <span class="{{ 'ok' if llm_ok else 'off' }}">Email AI {{ 'connected' if llm_ok else 'not configured' }}</span>
    <span class="{{ 'ok' if smtp_ok else 'off' }}">SMTP {{ 'connected' if smtp_ok else 'not configured' }}</span>
    <span class="{{ 'ok' if auto_send_master else 'off' }}">Auto-send {{ 'ENABLED (cap ' ~ cap ~ '/day, ' ~ sent_today ~ ' sent today)' if auto_send_master else 'off (drafts only)' }}</span>
  </div>

  <div class="card">
    <form method="post" action="{{ url_for('prospects_search') }}">
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("GEO_PORT", 8080)))
