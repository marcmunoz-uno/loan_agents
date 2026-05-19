"""
loan_officer/tests/conftest.py — Shared fixtures for the intake test suite.

Each test gets a fresh SQLite file in a tmpdir; we monkey-patch shared.db.DB_PATH
so init_db() writes there. The Flask app is built per-test so its DB binding
matches the patched path.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Per-test SQLite path. Returns the path; init_db() must be called by the test or fixture."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    import shared.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))

    db_mod.init_db()
    return db_path


@pytest.fixture
def app_client(temp_db, monkeypatch):
    """Flask test client with a fresh DB."""
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")

    # shared/auth.py reads TRANCHI_API_SECRET at module-import time, so monkey-
    # patching only the env var doesn't update the cached value when something
    # else imported shared.auth before this fixture ran.
    import shared.auth as auth_mod
    monkeypatch.setattr(auth_mod, "TRANCHI_API_SECRET", "test-secret")

    import app as app_module
    importlib.reload(app_module)  # rebind blueprints against the patched DB
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


@pytest.fixture
def auth_headers():
    return {
        "Authorization": "Bearer test-secret",
        "Content-Type": "application/json",
    }


@pytest.fixture
def stub_s3():
    """Configured-looking S3Client with all I/O methods mocked. Returned for assertion access."""
    from itertools import count
    from unittest.mock import MagicMock

    from shared.s3_client import S3Client, PresignedUpload

    client = S3Client(bucket="test-bucket", region="us-east-1", key_prefix="intake/")
    counter = count(1)

    def _presign(**kw):
        n = next(counter)
        doc_id = f"doc_test_{n:016d}"
        return PresignedUpload(
            doc_id=doc_id,
            bucket="test-bucket",
            s3_key=f"intake/deals/{kw['deal_id']}/docs/{doc_id}/{kw['filename']}",
            upload_url=f"https://test-bucket.s3.amazonaws.com/signed-put-{n}",
            expires_in=900,
        )

    client.generate_presigned_put = MagicMock(side_effect=_presign)
    client.head_object = MagicMock(return_value={"ContentLength": 12345})
    client.get_object_bytes = MagicMock(return_value=b"fake-pdf-bytes")
    client.put_object_bytes = MagicMock()
    client.generate_presigned_get = MagicMock(return_value="https://test-bucket.s3.amazonaws.com/signed-get")
    client.delete_object = MagicMock()
    return client
