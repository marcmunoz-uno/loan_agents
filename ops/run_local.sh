#!/bin/bash
# ops/run_local.sh — Boot the loan_agents server on :5010 with SQLite
set -e

cd "$(dirname "$0")/.."

# Create venv if missing
if [ ! -d ".venv" ]; then
    echo "[run_local] Creating virtual environment..."
    python3 -m venv .venv
fi

# Install dependencies
echo "[run_local] Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

# Copy .env.example to .env if no .env exists
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "[run_local] Created .env from .env.example — add your ANTHROPIC_API_KEY to enable AI responses."
fi

# Create data dir
mkdir -p data

# Seed sample data
echo "[run_local] Seeding sample data..."
.venv/bin/python ops/seed_data.py

echo ""
echo "[run_local] Starting server on http://localhost:5010 ..."
echo "[run_local] Press Ctrl+C to stop."
echo ""

PORT=5010 .venv/bin/python app.py
