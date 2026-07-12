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


init_db()


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


def _report_path(audit_id, ext):
    p = os.path.join(REPORT_DIR, f"{audit_id}.{ext}")
    return p if os.path.exists(p) else None


@app.route("/report/<int:audit_id>")
@login_required
def report(audit_id):
    p = _report_path(audit_id, "html")
    if not p:
        abort(404)
    with open(p, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.route("/pdf/<int:audit_id>")
@login_required
def pdf(audit_id):
    p = _report_path(audit_id, "pdf")
    if not p:
        abort(404)
    with db() as conn:
        row = conn.execute("SELECT url FROM audits WHERE id=?", (audit_id,)).fetchone()
    name = "ai-visibility-audit"
    if row:
        name = "ai-visibility-audit-" + urlparse(row["url"]).netloc.replace(":", "_")
    return send_file(p, as_attachment=True, download_name=f"{name}.pdf")


@app.route("/delete/<int:audit_id>", methods=["POST"])
@login_required
def delete(audit_id):
    for ext in ("html", "pdf", "json"):
        p = _report_path(audit_id, ext)
        if p:
            os.remove(p)
    with _db_lock, db() as conn:
        conn.execute("DELETE FROM audits WHERE id=?", (audit_id,))
    return redirect(url_for("index"))


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
  .links a { color:#1d4ed8; text-decoration:none; font-weight:600; margin-right:10px; }
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
  <h1>AI Visibility Audit</h1><a href="{{ url_for('logout') }}">Log out</a>
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
      <tr><th>Site</th><th>Date</th><th>Score</th><th>Status</th><th></th><th></th></tr>
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
        <td class="links">
          {% if r['status']=='done' %}
            <a href="{{ url_for('report', audit_id=r['id']) }}" target="_blank">View</a>
            {% if r['pdf'] %}<a href="{{ url_for('pdf', audit_id=r['id']) }}">PDF</a>{% endif %}
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
      {% if not rows %}<tr><td colspan="6" style="color:#94a3b8">
        No audits yet. Enter a URL above and click Run Audit.</td></tr>{% endif %}
    </table>
  </div>
</div></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("GEO_PORT", 8080)))
