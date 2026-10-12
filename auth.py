"""Registration, login and logout (server-rendered forms with CSRF protection)."""
import re
import secrets
import time
from collections import defaultdict, deque

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
from util import now_utc, to_str

bp = Blueprint("auth", __name__)

_hits = defaultdict(deque)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def rate_limited(key, limit, window):
    if not current_app.config.get("RATELIMIT_ENABLED", True):
        return False
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def ensure_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(24)
    return session["csrf"]


def _check_form_csrf():
    token = request.form.get("csrf_token", "")
    return bool(token) and secrets.compare_digest(token, session.get("csrf", ""))


def _ip():
    """Client IP. X-Forwarded-For is only trusted behind a known proxy (Render / TRUST_PROXY=1),
    and then only its LAST entry, which is the one the trusted proxy appended."""
    if current_app.config.get("TRUST_PROXY"):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.remote_addr or "?"


@bp.route("/register", methods=["GET", "POST"])
def register():
    if session.get("uid"):
        return redirect(url_for("index"))
    error = None
    ensure_csrf()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not _check_form_csrf():
            error = "Your session expired. Please try again."
        elif rate_limited("reg:" + _ip(), 10, 3600):
            error = "Too many sign-up attempts. Please wait a while."
        elif not name or len(name) > 60:
            error = "Please enter your name (up to 60 characters)."
        elif not EMAIL_RE.match(email):
            error = "Please enter a valid email address."
        elif len(pw) < 8 or len(pw) > 128:
            error = "Password must be 8-128 characters."
        elif db.one("SELECT 1 FROM tf_users WHERE email=?", (email,)):
            error = "An account with this email already exists."
        else:
            cur = db.run("INSERT INTO tf_users(email,name,password_hash,created_at) VALUES(?,?,?,?)",
                         (email, name, generate_password_hash(pw), to_str(now_utc())))
            uid = cur.lastrowid
            db.get_db().commit()
            session.clear()
            session["uid"] = uid
            session["csrf"] = secrets.token_hex(24)
            session.permanent = True
            return redirect(url_for("index"))
    return render_template("register.html", error=error), (400 if error else 200)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid"):
        return redirect(url_for("index"))
    error = None
    ensure_csrf()
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not _check_form_csrf():
            error = "Your session expired. Please try again."
        elif rate_limited("login:" + _ip() + email, 8, 300) or rate_limited("loginemail:" + email, 25, 900):
            error = "Too many attempts. Please wait a few minutes."
        else:
            u = db.one("SELECT * FROM tf_users WHERE email=?", (email,))
            if u and check_password_hash(u["password_hash"], pw):
                session.clear()
                session["uid"] = u["id"]
                session["csrf"] = secrets.token_hex(24)
                session.permanent = True
                return redirect(url_for("index"))
            error = "Incorrect email or password."
    return render_template("login.html", error=error), (400 if error else 200)


@bp.post("/logout")
def logout():
    if _check_form_csrf():
        session.clear()
    return redirect(url_for("auth.login"))
