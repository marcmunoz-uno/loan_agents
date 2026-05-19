"""
shared/s3_client.py — Thin boto3 wrapper for intake-document uploads.

Surfaces only what the intake pipeline needs:
  - generate_presigned_put(...)   borrower uploads directly to S3
  - generate_presigned_get(...)   short-lived URL for re-reading
  - get_object_bytes(s3_key)      pulls the file for OCR (Claude vision)
  - delete_object(s3_key)         cleanup on failed flows

Configured from env:
    AWS_REGION             default us-east-1
    AWS_S3_BUCKET          required for any non-trivial call
    AWS_ACCESS_KEY_ID      standard boto3 credential discovery still works
    AWS_SECRET_ACCESS_KEY  same; instance profiles + ~/.aws/credentials also fine

When AWS_S3_BUCKET is unset the client treats itself as `disabled` — every
public method raises S3NotConfigured. Routes should surface a 503 in that case
so dev environments without S3 credentials still boot cleanly.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
DEFAULT_KEY_PREFIX = os.environ.get("AWS_S3_KEY_PREFIX", "intake/")
DEFAULT_EXPIRES_PUT = int(os.environ.get("AWS_S3_PUT_EXPIRES", "900"))   # 15 min
DEFAULT_EXPIRES_GET = int(os.environ.get("AWS_S3_GET_EXPIRES", "3600"))  # 1 hr


class S3NotConfigured(RuntimeError):
    """Raised when an S3 call is attempted without an AWS_S3_BUCKET configured."""


@dataclass
class PresignedUpload:
    """Result of a presigned PUT request."""
    doc_id: str
    bucket: str
    s3_key: str
    upload_url: str
    expires_in: int


# ── Client ────────────────────────────────────────────────────────────────────

class S3Client:
    def __init__(
        self,
        bucket: str = DEFAULT_BUCKET,
        region: str = DEFAULT_REGION,
        key_prefix: str = DEFAULT_KEY_PREFIX,
    ):
        self.bucket = bucket
        self.region = region
        self.key_prefix = key_prefix.rstrip("/") + "/" if key_prefix else ""
        self._boto_client = None

    @property
    def configured(self) -> bool:
        return bool(self.bucket)

    def _client(self):
        """Lazy boto3 client. Imported inside so the dependency is optional in dev."""
        if not self.configured:
            raise S3NotConfigured("AWS_S3_BUCKET is not set")
        if self._boto_client is None:
            import boto3  # noqa: WPS433  (intentionally local import)
            self._boto_client = boto3.client("s3", region_name=self.region)
        return self._boto_client

    # ── Key construction ──────────────────────────────────────────────────────

    def build_key(self, deal_id: str, doc_id: str, filename: str) -> str:
        """deals/{deal_id}/docs/{doc_id}/{safe_filename}"""
        safe = _sanitize_filename(filename)
        return f"{self.key_prefix}deals/{deal_id or 'unattached'}/docs/{doc_id}/{safe}"

    # ── Presign ──────────────────────────────────────────────────────────────

    def generate_presigned_put(
        self,
        *,
        deal_id: str,
        filename: str,
        content_type: str,
        doc_id: Optional[str] = None,
        expires_in: int = DEFAULT_EXPIRES_PUT,
    ) -> PresignedUpload:
        """
        Return a presigned URL the client can PUT a file to.

        Args:
            deal_id:      groups all docs for one borrower deal context
            filename:     raw user-supplied filename — sanitized into the s3 key
            content_type: must match the Content-Type header the client sends
            doc_id:       optional override (e.g. for retries); auto-generated otherwise
        """
        doc_id = doc_id or f"doc_{uuid.uuid4().hex[:16]}"
        s3_key = self.build_key(deal_id, doc_id, filename)
        url = self._client().generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": self.bucket,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
        return PresignedUpload(
            doc_id=doc_id,
            bucket=self.bucket,
            s3_key=s3_key,
            upload_url=url,
            expires_in=expires_in,
        )

    def generate_presigned_get(
        self,
        s3_key: str,
        expires_in: int = DEFAULT_EXPIRES_GET,
    ) -> str:
        return self._client().generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": s3_key},
            ExpiresIn=expires_in,
            HttpMethod="GET",
        )

    # ── Read / delete ─────────────────────────────────────────────────────────

    def get_object_bytes(self, s3_key: str) -> bytes:
        """Download the full object. Use sparingly — OCR needs this for PDF base64."""
        obj = self._client().get_object(Bucket=self.bucket, Key=s3_key)
        return obj["Body"].read()

    def head_object(self, s3_key: str) -> dict:
        return self._client().head_object(Bucket=self.bucket, Key=s3_key)

    def delete_object(self, s3_key: str) -> None:
        self._client().delete_object(Bucket=self.bucket, Key=s3_key)


# ── Helpers ───────────────────────────────────────────────────────────────────

_UNSAFE = {" ", "?", "&", "#", "%", "+", "=", "/", "\\", "..", "\t", "\n"}


def _sanitize_filename(name: str) -> str:
    """Replace shell-/URL-unfriendly chars and strip leading dots."""
    cleaned = name.lstrip(".").strip() or "file"
    for ch in _UNSAFE:
        cleaned = cleaned.replace(ch, "_")
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned


# Module-level singleton so callers don't redundantly recreate the client.
_default: Optional[S3Client] = None


def get_default_client() -> S3Client:
    global _default
    if _default is None:
        _default = S3Client()
    return _default
