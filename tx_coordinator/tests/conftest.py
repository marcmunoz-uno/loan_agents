"""
Shared pytest fixtures for the tx_coordinator test suite.

Every test gets its own SQLite file so suites can run in parallel and don't
collide with the dev DB at data/dealflow.db.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# Add repo root to sys.path so `import tx_coordinator.*` and `import shared.*` work
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point shared.db at a per-test SQLite file and run all migrations."""
    target = tmp_path / "tx_test.db"
    monkeypatch.setenv("DB_PATH", str(target))

    # shared.db reads DB_PATH at import time, so we have to patch the live attr.
    from shared import db as _db
    monkeypatch.setattr(_db, "DB_PATH", str(target))
    _db.init_db()
    yield target


TEST_SECRET = "test-secret-abc123"


@pytest.fixture
def client(db_path, monkeypatch):
    """Flask test client wired to the per-test DB with a known auth secret."""
    monkeypatch.setenv("TRANCHI_API_SECRET", TEST_SECRET)

    # auth reads the secret into a module global at import time; patch it live.
    from shared import auth as _auth
    monkeypatch.setattr(_auth, "TRANCHI_API_SECRET", TEST_SECRET)

    import app as _app_module
    flask_app = _app_module.create_app()
    # Cap uploads low so the oversized-upload test stays cheap (1MB).
    flask_app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
    flask_app.config.update(TESTING=True)
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {TEST_SECRET}"}


@pytest.fixture
def sample_psa():
    """A complete PSATerms blob with explicit psa_execution_date."""
    from shared.schemas import PSATerms
    return PSATerms(
        purchase_price=95000.0,
        earnest_money=2500.0,
        closing_date="2026-06-13",
        psa_execution_date="2026-05-14",
        inspection_period_days=10,
        financing_contingency_days=21,
        title_contingency_days=14,
        buyer_name="Marc Munoz",
        buyer_email="marc@munoz.ltd",
        buyer_phone="+13135550100",
        seller_name="John Smith",
        seller_email="jsmith@example.com",
        buyer_agent_name="Sarah Jones",
        listing_agent_name="Bob Williams",
        property_address="4521 Oak Ln, Detroit MI 48224",
    )


@pytest.fixture
def insert_transaction(db_path, sample_psa):
    """Create a transaction row + milestones + deadlines and return its id."""
    import uuid
    from shared.db import get_conn, insert
    from tx_coordinator.timeline import generate_timeline

    tx_id = f"tx_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    milestones = generate_timeline(sample_psa)

    with get_conn() as conn:
        insert(conn, "transactions", {
            "id": tx_id,
            "user_id": "usr_test",
            "psa_terms": sample_psa.model_dump_json(),
            "purchase_price": sample_psa.purchase_price,
            "closing_date": sample_psa.closing_date,
            "psa_execution_date": sample_psa.psa_execution_date,
            "status": "open",
            "current_milestone": "psa_executed",
            "property_address": sample_psa.property_address,
            "buyer_name": sample_psa.buyer_name,
            "seller_name": sample_psa.seller_name,
            "notes": "",
            "created_at": now,
            "updated_at": now,
        })
        for m in milestones:
            conn.execute(
                """INSERT INTO tx_milestones
                   (transaction_id, milestone_name, milestone_label, sequence_order,
                    target_date, status, notes)
                   VALUES (?, ?, ?, ?, ?, 'pending', '')""",
                (tx_id, m["name"], m["label"], m["sequence"], m["target_date"]),
            )
        for m in milestones:
            if m.get("is_contingency"):
                conn.execute(
                    """INSERT INTO tx_deadlines
                       (transaction_id, contingency_type, deadline_date, status)
                       VALUES (?, ?, ?, 'active')""",
                    (tx_id, m["contingency_type"], m["target_date"]),
                )
        conn.execute(
            """INSERT INTO tx_parties (transaction_id, party_type, name, email, phone, added_at)
               VALUES (?, 'buyer', ?, ?, ?, ?)""",
            (tx_id, sample_psa.buyer_name, sample_psa.buyer_email, sample_psa.buyer_phone, now),
        )
        conn.commit()

    return tx_id
