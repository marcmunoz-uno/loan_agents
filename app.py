"""
app.py — Flask entry point for loan_agents.

Registers two blueprints:
  - loan_officer    → /api/loan/*
  - loan_processor  → /api/processor/*

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
from loan_processor.routes import processor_bp


def create_app() -> Flask:
    app = Flask(__name__)

    try:
        init_db()
    except Exception as e:
        print(f"[app] DB init warning: {e}")

    app.register_blueprint(loan_bp)
    app.register_blueprint(processor_bp)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "ok",
            "service": "loan_agents",
            "agents": ["loan_officer", "loan_processor"],
            "personas": [
                "Tranchi - Loan Officer",
                "Tranchi - Loan Processor",
            ],
            "ts": datetime.now(timezone.utc).isoformat(),
        })

    @app.route("/", methods=["GET"])
    def root():
        return jsonify({
            "service": "loan_agents",
            "description": "AI Loan Officer + Loan Processor for Tranchi.ai",
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
            },
            "docs": "See loan_officer/README.md and loan_processor/README.md",
        })

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5010))
    app.run(host="0.0.0.0", port=port, debug=True)
