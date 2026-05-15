"""
app.py — Flask entry point for tranchi-deal-flow-agents.

Registers four blueprints:
  - loan_officer    → /api/loan/*
  - tx_coordinator  → /api/tx/*
  - loan_processor  → /api/processor/*
  - orchestrator    → /api/chat/*   (unified consumer chat front-door)

Deploy: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120
Local:  PORT=5010 python app.py
"""

import os
from datetime import datetime, timezone
from flask import Flask, jsonify

from dotenv import load_dotenv
load_dotenv()

from shared.db import init_db
from loan_officer.routes import loan_bp
from tx_coordinator.routes import tx_bp
from loan_processor.routes import processor_bp
from orchestrator.routes import chat_bp


def create_app() -> Flask:
    app = Flask(__name__)

    # Initialize DB tables on first run
    try:
        init_db()
    except Exception as e:
        print(f"[app] DB init warning: {e}")

    # Register blueprints
    app.register_blueprint(loan_bp)
    app.register_blueprint(tx_bp)
    app.register_blueprint(processor_bp)
    app.register_blueprint(chat_bp)

    # ── Health check ──────────────────────────────────────────────────────────

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "ok",
            "service": "tranchi-deal-flow-agents",
            "agents": ["loan_officer", "tx_coordinator", "loan_processor"],
            "personas": [
                "Tranchi - Loan Officer",
                "Tranchi - Loan Processor",
                "Tranchi - Transaction Coordinator",
            ],
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    # ── Root ──────────────────────────────────────────────────────────────────

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "service": "tranchi-deal-flow-agents",
            "description": "AI Loan Officer + Loan Processor + Transaction Coordinator for Tranchi.ai",
            "endpoints": {
                "health": "GET /health",
                "loan_officer": {
                    "prequal": "POST /api/loan/prequal",
                    "application": "POST /api/loan/application",
                    "chat": "POST /api/loan/chat",
                    "arive_webhook": "POST /api/loan/webhook/arive-update",
                },
                "loan_processor": {
                    "pre_underwrite": "POST /api/processor/pre-underwrite/<app_id>",
                    "guidelines": "GET /api/processor/guidelines",
                    "chat": "POST /api/processor/chat",
                },
                "tx_coordinator": {
                    "open": "POST /api/tx/open",
                    "status": "GET /api/tx/<tx_id>",
                    "chat": "POST /api/tx/<tx_id>/chat",
                },
                "orchestrator": {
                    "turn": "POST /api/chat/turn",
                    "personas": "GET /api/chat/personas",
                },
            },
            "docs": "See loan_officer/README.md, loan_processor/README.md, and tx_coordinator/README.md",
        })

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    app.run(host="0.0.0.0", port=port, debug=True)
