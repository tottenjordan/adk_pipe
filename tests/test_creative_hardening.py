"""Hardening of creative_agent's untrusted inputs: the research PDF's Markdown,
reference-image fetches (SSRF guard, gs:// size cap) and the image retry rule."""

import httpx
import pymupdf
import pytest
from google.api_core import exceptions as gapi_exceptions
from google.genai import errors as genai_errors

from creative_agent import gcs_tools, image_tools, references

# --- research PDF -------------------------------------------------------------


def _png() -> bytes:
    return pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False).tobytes(
        "png"
    )


def _pdf_text(path) -> str:
    with pymupdf.open(path) as doc:
        return "".join(page.get_text() for page in doc)


def test_report_pdf_escapes_raw_html(tmp_path):
    report = (
        "# Report\n\n"
        '<img src="https://evil.example/x.png">\n\n'
        "<script>alert(1)</script>\n\n"
        'See <a href="file:///etc/passwd">this</a> inline.\n'
    )
    out = tmp_path / "r.pdf"
    html = gcs_tools.render_report_pdf(report, str(out))

    assert "<img" not in html and "<script" not in html and "<a href" not in html
    assert "&lt;img src=&quot;https://evil.example/x.png&quot;&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;a href=&quot;file:///etc/passwd&quot;&gt;this&lt;/a&gt;" in html
    text = _pdf_text(out)
    assert "<script>alert(1)</script>" in text  # shown as literal text
    assert "etc/passwd" in text and ">this</a>" in text


def test_report_pdf_keeps_normal_markdown(tmp_path):
    report = (
        "# Report\n\n## Executive Summary\n\n"
        "- **Bold** point\n- [Source](https://example.com/a)\n\n"
        "| Col A | Col B |\n|---|---|\n| 1 | 2 |\n"
    )
    out = tmp_path / "r.pdf"
    html = gcs_tools.render_report_pdf(report, str(out))
    assert "<h1>Report</h1>" in html and "<h2>Executive Summary</h2>" in html
    assert "<li><strong>Bold</strong> point</li>" in html
    assert '<a href="https://example.com/a">Source</a>' in html
    assert "<table>" in html and "<td>1</td>" in html
    text = _pdf_text(out)
    assert "Executive Summary" in text and "Col B" in text


def test_report_pdf_does_not_read_images_from_the_cwd(tmp_path, monkeypatch):
    """The PyMuPDF Story archive is an empty temp dir, not the process cwd, so a
    Markdown image naming a local file (relative or absolute) is never embedded."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "secret.png").write_bytes(_png())
    monkeypatch.chdir(cwd)
    out = tmp_path / "r.pdf"
    gcs_tools.render_report_pdf(
        f"# R\n\n![a](secret.png)\n\n![b]({cwd / 'secret.png'})\n", str(out)
    )
    with pymupdf.open(out) as doc:
        assert [len(page.get_images()) for page in doc] == [0]


# --- SSRF guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("100.64.0.1", False),  # shared address space (RFC 6598)
        ("100.127.255.254", False),
        ("64:ff9b::a9fe:a9fe", False),  # NAT64 of 169.254.169.254
        ("169.254.169.254", False),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("::1", False),
        ("::ffff:10.0.0.1", False),
        ("fe80::1%eth0", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
    ],
)
def test_is_public_ip(ip, public):
    assert references._is_public_ip(ip) is public


def test_shared_address_space_host_is_refused(monkeypatch):
    monkeypatch.setattr(
        references.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(2, 1, 6, "", ("100.64.0.1", port))],
    )
    with pytest.raises(references.UnsafeReferenceURL):
        references._validate_reference_url("https://cgnat.example/a.png")


# --- gs:// size cap -----------------------------------------------------------


class _Blob:
    def __init__(self, size, data=b"img"):
        self.size = size
        self.data = data
        self.download_calls = []

    def download_as_bytes(self, **kwargs):
        self.download_calls.append(kwargs)
        return self.data


class _Bucket:
    def __init__(self, blob):
        self.blob_ = blob
        self.get_calls = []

    def get_blob(self, name, **kwargs):
        self.get_calls.append((name, kwargs))
        return self.blob_

    def blob(self, name):  # the old unchecked path must not be used
        raise AssertionError("download must go through get_blob")


def _patch_gcs(monkeypatch, blob):
    bucket = _Bucket(blob)
    monkeypatch.setattr(
        gcs_tools,
        "_get_gcs_client",
        lambda: type("C", (), {"bucket": lambda s, n: bucket})(),
    )
    return bucket


def test_download_blob_checks_size_before_downloading(monkeypatch):
    blob = _Blob(size=gcs_tools.DOWNLOAD_MAX_BYTES + 1)
    bucket = _patch_gcs(monkeypatch, blob)
    with pytest.raises(ValueError, match="larger than"):
        gcs_tools._download_blob("b", "big.png")
    assert blob.download_calls == []  # never fetched
    ((name, kwargs),) = bucket.get_calls
    assert name == "big.png" and kwargs["timeout"] == gcs_tools.GCS_DOWNLOAD_TIMEOUT


def test_download_blob_within_cap_downloads_with_timeout(monkeypatch):
    blob = _Blob(size=gcs_tools.DOWNLOAD_MAX_BYTES, data=b"ok")
    _patch_gcs(monkeypatch, blob)
    assert gcs_tools._download_blob("b", "ok.png") == b"ok"
    assert blob.download_calls == [{"timeout": gcs_tools.GCS_DOWNLOAD_TIMEOUT}]


def test_download_blob_caps_the_bytes_actually_read(monkeypatch):
    # The object grew between metadata read and download: still refused.
    blob = _Blob(size=3, data=b"x" * (gcs_tools.DOWNLOAD_MAX_BYTES + 1))
    _patch_gcs(monkeypatch, blob)
    with pytest.raises(ValueError, match="larger than"):
        gcs_tools._download_blob("b", "grew.png")


def test_download_blob_missing_object_raises_not_found(monkeypatch):
    _patch_gcs(monkeypatch, None)
    with pytest.raises(gapi_exceptions.NotFound):
        gcs_tools._download_blob("b", "gone.png")


def test_oversized_gs_reference_is_skipped_without_download(monkeypatch):
    blob = _Blob(size=50 * 1024 * 1024)
    _patch_gcs(monkeypatch, blob)
    assert references.fetch_reference_image("gs://b/huge.png") is None
    assert blob.download_calls == []


# --- image retry predicate ----------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "retry"),
    [
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("t"), True),
        (genai_errors.ClientError(408, {"error": {"message": "timeout"}}), True),
        (genai_errors.ClientError(429, {"error": {"message": "quota"}}), True),
        (genai_errors.ServerError(503, {"error": {"message": "down"}}), True),
        (genai_errors.ClientError(400, {"error": {"message": "bad"}}), False),
        (genai_errors.ClientError(403, {"error": {"message": "no"}}), False),
        (ValueError("x"), False),
    ],
)
def test_image_retry_predicate(exc, retry):
    assert image_tools._is_retryable_genai_error(exc) is retry


def test_report_markdown_preset_disables_html_twice():
    """Both layers stay: the html option and the html_block/html_inline rules."""
    preset = gcs_tools._report_markdown_preset()
    assert preset["options"]["html"] is False
    assert "html_block" not in preset["components"]["block"]["rules"]
    assert "html_inline" not in preset["components"]["inline"]["rules"]
    assert "table" not in preset["components"]["block"]["rules"]  # enabled by
    # MarkdownPdf itself; the rest of commonmark is unchanged
