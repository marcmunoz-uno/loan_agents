"""shared/tests/test_net.py — SSRF URL guard."""

import pytest

from shared.net import assert_safe_url, UnsafeURLError


@pytest.mark.parametrize("url", [
    "https://api.typeform.com/responses/files/abc/x.pdf",
    "https://files.zapier.com/x.pdf",
    "http://files.example.com/x.pdf",
])
def test_allows_public_urls(url):
    assert_safe_url(url)  # should not raise


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:5010/api/loan/prequal",
    "http://127.0.0.1/x",
    "http://10.0.0.5/x",
    "http://192.168.1.10/x",
    "http://[::1]/x",
    "https://metadata.google.internal/computeMetadata/v1/",
    "not-a-url",
])
def test_blocks_unsafe_urls(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)
