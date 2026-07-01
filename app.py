"""
app.py — Flask entry point for loan_agents.

Registers three blueprints:
  - loan_officer    → /api/loan/*
  - loan_processor  → /api/processor/*
  - intake          → /api/intake/*   (S3 upload + Claude vision OCR pipeline)

Deploy: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
Local:  PORT=5010 python app.py
"""

import logging
import os
from datetime import datetime, timezone
from flask import Flask, jsonify

from dotenv import load_dotenv
load_dotenv()

from shared.config import is_production, startup_warnings
from shared.auth import assert_secret_configured
from shared.db import init_db, get_conn
from loan_officer.routes import loan_bp
from loan_officer.intake.routes import intake_bp
from loan_officer.typeform.webhook import typeform_webhook_bp
from loan_processor.routes import processor_bp
from tx_coordinator.routes import tx_bp

# Cap request bodies (notably PSA PDF uploads to /api/tx/open-from-pdf) so a
# large upload can't OOM the instance. Override with MAX_CONTENT_LENGTH_MB.
MAX_CONTENT_LENGTH_MB = int(os.environ.get("MAX_CONTENT_LENGTH_MB", "25"))

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH_MB * 1024 * 1024

    # Refuse to boot in prod with the default/blank dev secret (auth + HMAC
    # signing depend on it). Non-fatal config gaps are logged, not raised.
    assert_secret_configured()
    for w in startup_warnings():
        logger.warning("[config] %s", w)

    # A broken schema must stop the deploy — don't boot and serve 500s on every
    # DB-touching request behind a green health check.
    init_db()

    app.register_blueprint(loan_bp)
    app.register_blueprint(intake_bp)
    app.register_blueprint(typeform_webhook_bp)
    app.register_blueprint(processor_bp)
    app.register_blueprint(tx_bp)

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({
            "error": "payload_too_large",
            "max_mb": MAX_CONTENT_LENGTH_MB,
        }), 413

    @app.errorhandler(Exception)
    def handle_uncaught(e):
        # Always return JSON (API clients break on Flask's default HTML 500),
        # and never leak internals to callers in production.
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description}), e.code
        logger.exception("unhandled exception")
        detail = "internal server error" if is_production() else f"{type(e).__name__}: {e}"
        return jsonify({"error": detail}), 500

    @app.route("/health", methods=["GET"])
    def health():
        # Probe the DB so Render doesn't route traffic to an instance whose
        # disk/schema is broken.
        db_ok = True
        try:
            with get_conn() as conn:
                conn.execute("SELECT 1")
        except Exception:
            db_ok = False
        return jsonify({
            "status": "ok" if db_ok else "degraded",
            "db": "ok" if db_ok else "error",
            "service": "loan_agents",
            "agents": ["loan_officer", "loan_processor", "intake", "tx_coordinator"],
            "personas": [
                "Tranchi - Loan Officer",
                "Tranchi - Loan Processor",
                "Tranchi - Transaction Coordinator (Sam)",
            ],
            "agent_mode": os.environ.get("TX_AGENT_MODE", "shadow"),
            "ts": datetime.now(timezone.utc).isoformat(),
        }), (200 if db_ok else 503)

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "service": "loan_agents",
            "description": "AI Loan Officer + Loan Processor + intake pipeline for Tranchi.ai",
            "endpoints": {
                "health": "GET /health",
                "loan_officer": {
                    "prequal":          "POST /api/loan/prequal",
                    "application":      "POST /api/loan/application",
                    "chat":             "POST /api/loan/chat",
                    "prequal_letter":   "POST /api/loan/prequal-letter/<prequal_id>",
                    "letter_audit":     "GET  /api/loan/prequal-letter/<letter_id>",
                    "letter_pdf":       "GET  /api/loan/prequal-letter/<letter_id>/pdf?token=<hmac>&exp=<unix>",
                    "arive_webhook":    "POST /api/loan/webhook/arive-update",
                    "typeform_webhook": "POST /api/loan/webhook/typeform-submit",
                },
                "loan_processor": {
                    "pre_underwrite": "POST /api/processor/pre-underwrite/<app_id>",
                    "guidelines": "GET /api/processor/guidelines",
                    "chat": "POST /api/processor/chat",
                },
                "intake": {
                    "presign":          "POST /api/intake/upload/presign",
                    "confirm":          "POST /api/intake/upload/confirm",
                    "classify":         "POST /api/intake/upload/<doc_id>/classify",
                    "status":           "GET  /api/intake/upload/<doc_id>",
                    "attach":           "POST /api/intake/upload/<doc_id>/attach",
                    "inbound_email":    "POST /api/intake/inbound-email-attachment",
                    "by_deal":          "GET  /api/intake/deals/<deal_id>/docs",
                    "by_app":           "GET  /api/intake/applications/<app_id>/docs",
                    "completeness":     "GET  /api/intake/applications/<app_id>/completeness?product=dscr",
                    "ocr_statements":   "POST /api/intake/ocr-statements",
                },
                "tx_coordinator": {
                    "open":            "POST /api/tx/open",
                    "open_from_pdf":   "POST /api/tx/open-from-pdf",
                    "get":             "GET  /api/tx/<tx_id>",
                    "complete":        "POST /api/tx/<tx_id>/milestone/<name>/complete",
                    "party":           "POST /api/tx/<tx_id>/party",
                    "deadlines":       "GET  /api/tx/<tx_id>/deadlines",
                    "document":        "POST /api/tx/<tx_id>/document",
                    "communication":   "POST /api/tx/<tx_id>/communication",
                    "chat":            "POST /api/tx/<tx_id>/chat",
                    "sweep":           "POST /api/tx/sweep",
                    "order_title":     "POST /api/tx/<tx_id>/order-title",
                    "notify":          "POST /api/tx/<tx_id>/notify",
                    "sync_arive":      "POST /api/tx/<tx_id>/sync-arive-contacts",
                    "link_arive_loan": "POST /api/tx/<tx_id>/link-arive-loan",
                    "guardrails":      "GET  /api/tx/guardrails",
                    "pause":           "POST /api/tx/pause   (kill switch on)",
                    "resume":          "POST /api/tx/resume  (kill switch off)",
                    "go_live":         "POST /api/tx/<tx_id>/go-live",
                    "go_shadow":       "POST /api/tx/<tx_id>/go-shadow",
                },
            },
            "docs": "See loan_officer/README.md, loan_processor/README.md, tx_coordinator/README.md",
        })

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    # Never enable the Werkzeug debugger in production — it's a remote shell.
    debug = os.environ.get("FLASK_DEBUG", "0") == "1" and not is_production()
    app.run(host="0.0.0.0", port=port, debug=debug)
