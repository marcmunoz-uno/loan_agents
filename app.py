"""
app.py — Flask entry point for loan_agents.

Registers three blueprints:
  - loan_officer    → /api/loan/*
  - loan_processor  → /api/processor/*
  - intake          → /api/intake/*   (S3 upload + Claude vision OCR pipeline)

Deploy: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
Local:  PORT=5010 python app.py
"""

import os
from datetime import datetime, timezone
from flask import Flask, jsonify

from dotenv import load_dotenv
load_dotenv()

from shared.auth import assert_secret_configured
from shared.db import init_db
from loan_officer.routes import loan_bp
from loan_officer.intake.routes import intake_bp
from loan_officer.typeform.webhook import typeform_webhook_bp
from loan_processor.routes import processor_bp
from tx_coordinator.routes import tx_bp

# Cap request bodies (notably PSA PDF uploads to /api/tx/open-from-pdf) so a
# large upload can't OOM the instance. Override with MAX_CONTENT_LENGTH_MB.
MAX_CONTENT_LENGTH_MB = int(os.environ.get("MAX_CONTENT_LENGTH_MB", "25"))


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH_MB * 1024 * 1024

    # Refuse to boot in prod with the default dev secret.
    assert_secret_configured()

    try:
        init_db()
    except Exception as e:
        print(f"[app] DB init warning: {e}")

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

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "ok",
            "service": "loan_agents",
            "agents": ["loan_officer", "loan_processor", "intake", "tx_coordinator"],
            "personas": [
                "Tranchi - Loan Officer",
                "Tranchi - Loan Processor",
                "Tranchi - Transaction Coordinator (Sam)",
            ],
            "agent_mode": os.environ.get("TX_AGENT_MODE", "shadow"),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

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
    app.run(host="0.0.0.0", port=port, debug=True)
