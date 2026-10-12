"""TaskFlow - intelligent productivity workspace (Flask + SQLite).

Run locally:   python app.py
Production:    gunicorn app:app
"""
import os
import secrets
from datetime import timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

import api
import auth
import db


def _secret_key(instance_path):
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    # No env secret: persist a generated one so sessions survive restarts / multiple workers.
    os.makedirs(instance_path, exist_ok=True)
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        DB_PATH=os.environ.get("DB_PATH") or os.path.join(app.instance_path, "taskflow.db"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")) or os.environ.get("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
        TRUST_PROXY=bool(os.environ.get("RENDER")) or os.environ.get("TRUST_PROXY") == "1",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _secret_key(app.instance_path)

    db.init_db(app.config["DB_PATH"])
    app.teardown_appcontext(db.close_db)
    app.register_blueprint(auth.bp)
    app.register_blueprint(api.bp)

    @app.context_processor
    def inject():
        return {"csrf_token": auth.ensure_csrf()}

    @app.after_request
    def finish(resp):
        # one transaction per request: commit on success, roll back on error
        conn = g.get("db")
        if conn is not None:
            if resp.status_code < 400:
                conn.commit()
            else:
                conn.rollback()
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        if not session.get("uid"):
            return redirect(url_for("auth.login"))
        return render_template("app.html")

    @app.get("/health")
    def health():
        try:
            db.get_db().execute("SELECT 1").fetchone()
        except Exception:
            return jsonify(status="error"), 503
        return jsonify(status="ok")

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return render_template("error.html", code=404, message="That page doesn't exist."), 404

    @app.errorhandler(405)
    def bad_method(_e):
        return jsonify(error="Method not allowed"), 405

    @app.errorhandler(413)
    def too_big(_e):
        return jsonify(error="Request too large"), 413

    @app.errorhandler(500)
    def server_error(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Something went wrong on our side"), 500
        return render_template("error.html", code=500, message="Something went wrong on our side."), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
