"""Reference images for image generation: which ones, in what order, and fetching them.

Callers seed up to ``MAX_REFERENCE_IMAGES`` reference images via createSession
initialState as ``reference_images`` (a list of ``{"uri", "role"}`` objects, or
its JSON string). The legacy single-reference keys ``reference_image_uri`` +
``reference_image_role`` are folded in as the first entry. The resolution
helpers are pure (no SDK imports), so both ``image_tools`` (rendering) and
``callbacks`` (state init) can use them; the SSRF-hardened fetch
(``fetch_references``) imports the genai types and the GCS download lazily.
"""

import asyncio
import ipaddress
import json
import logging
import socket
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

# Google allows up to 14 reference images; keep it small for render latency.
MAX_REFERENCE_IMAGES = 3

# How a reference image may be used. Only the legacy single pair may leave the
# role empty (→ `product`, the historical meaning of the single reference
# image); every ``reference_images`` entry must name its role.
REFERENCE_ROLES = ("product", "logo", "style")
_LEGACY_DEFAULT_ROLE = "product"


def _normalise_role(role: Any, default: str | None = None) -> str | None:
    """A valid role, ``default`` for a missing/empty one, else None."""
    if role is None:
        return default
    if not isinstance(role, str):
        return None
    role = role.strip().lower()
    if not role:
        return default
    return role if role in REFERENCE_ROLES else None


def _listed_references(raw: Any) -> list[Any]:
    """The raw ``reference_images`` value as a list (JSON strings parsed)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def resolve_references(state: Any) -> list[tuple[str, str]]:
    """Ordered, deduped, capped ``(uri, role)`` reference pairs from state.

    The legacy ``reference_image_uri`` (+ ``reference_image_role``, empty →
    product) comes first, then the ``reference_images`` entries in order.
    Entries without a non-empty string ``uri`` are skipped; list entries
    without a valid role are skipped with a warning; a repeated uri keeps its
    first occurrence; at most ``MAX_REFERENCE_IMAGES`` are returned.
    """
    candidates: list[tuple[Any, str | None]] = [
        (
            {
                "uri": state.get("reference_image_uri"),
                "role": state.get("reference_image_role"),
            },
            _LEGACY_DEFAULT_ROLE,
        )
    ]
    candidates.extend(
        (entry, None) for entry in _listed_references(state.get("reference_images"))
    )

    refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry, default_role in candidates:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            continue
        uri = uri.strip()
        role = _normalise_role(entry.get("role"), default_role)
        if role is None:
            logging.warning(
                f"Skipping reference image '{uri}': role {entry.get('role')!r} "
                f"is not one of {REFERENCE_ROLES}"
            )
            continue
        if uri in seen:
            continue
        seen.add(uri)
        refs.append((uri, role))
        if len(refs) == MAX_REFERENCE_IMAGES:
            break
    return refs


def reference_roles_summary(state: Any) -> str:
    """The resolved references' roles in order, e.g. ``"product, style"``."""
    return ", ".join(role for _, role in resolve_references(state))


# --- Fetching (SSRF-hardened) -------------------------------------------------------

# Fetch timeout for an http(s) reference image (stdlib urllib, no new dep).
REFERENCE_FETCH_TIMEOUT_SECS = 20

# Largest reference image accepted (http(s) body or gs:// object; image_tools
# applies the same cap to the person photo).
REFERENCE_MAX_BYTES = 10 * 1024 * 1024

# Map a reference-image extension to a mime type (default image/png).
_REFERENCE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# ``(bucket, object) -> bytes``; ``image_tools`` passes its ``_download_blob``.
Download = Callable[[str, str], bytes]


def reference_mime_for(path: str) -> str:
    """Best-effort mime type from a reference-image path/URL extension."""
    lower = path.lower()
    for ext, mime in _REFERENCE_MIME_BY_EXT.items():
        if lower.endswith(ext):
            return mime
    return "image/png"


class UnsafeReferenceURL(ValueError):
    """An http(s) reference URL that must not be fetched (SSRF guard)."""


def _is_public_ip(raw: str) -> bool:
    """False for loopback/private/link-local/reserved/multicast/unspecified and
    any other non-global address (e.g. 100.64.0.0/10 shared address space).

    The explicit checks stay alongside ``is_global``: some special-purpose
    addresses report ``is_global`` True (e.g. ``64:ff9b::a9fe:a9fe``, the NAT64
    form of the metadata address, is caught only by ``is_reserved``)."""
    ip = ipaddress.ip_address(raw.split("%", 1)[0])  # drop an IPv6 zone id
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _validate_reference_url(url: str) -> None:
    """Raise UnsafeReferenceURL unless ``url`` is http(s) to a public host.

    Every address the hostname resolves to must be public (a mixed answer is
    refused). Note: urllib re-resolves on connect, so this does not pin the
    address (DNS-rebinding window); it blocks the plain metadata/private-host
    cases a user-supplied URL could otherwise reach.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeReferenceURL(f"not an http(s) URL with a host: '{url}'")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not infos:
        raise UnsafeReferenceURL(f"'{parsed.hostname}' did not resolve")
    for info in infos:
        address = str(info[4][0])
        if not _is_public_ip(address):
            raise UnsafeReferenceURL(
                f"'{parsed.hostname}' resolves to non-public address {address}"
            )


class _RevalidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow at most 3 redirects, re-validating every hop's URL."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_reference_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _reference_opener() -> urllib.request.OpenerDirector:
    """A urllib opener whose redirect handler re-validates each hop."""
    return urllib.request.build_opener(_RevalidatingRedirectHandler)


def _fetch_http_reference(uri: str) -> tuple[bytes, str]:
    """(bytes, mime) of a public http(s) image ≤ the size cap, else raise."""
    _validate_reference_url(uri)
    with _reference_opener().open(uri, timeout=REFERENCE_FETCH_TIMEOUT_SECS) as resp:
        mime = resp.headers.get_content_type()
        if not mime.startswith("image/"):
            raise ValueError(f"Content-Type {mime!r} is not an image")
        data = resp.read(REFERENCE_MAX_BYTES + 1)
    if len(data) > REFERENCE_MAX_BYTES:
        raise ValueError(f"larger than {REFERENCE_MAX_BYTES} bytes")
    return data, mime


def _default_download(bucket: str, obj: str) -> bytes:
    # Imported lazily: gcs_tools pulls in the PDF stack, which the pure
    # resolution helpers above (used at state init) don't need.
    from .gcs_tools import _download_blob

    return _download_blob(bucket, obj)


def fetch_reference_image(uri: str, download: Download | None = None) -> Any:
    """Fetch a product/brand reference image as a genai Part, or None on failure.

    Supports ``gs://bucket/object`` (via ``download``, default the GCS client's
    capped ``gcs_tools._download_blob``) and ``http(s)://`` URLs (via stdlib
    urllib, hardened: public hosts only, ≤ 3 re-validated redirects, an
    ``image/*`` Content-Type). Both are capped at ``REFERENCE_MAX_BYTES``. Any
    failure is logged and swallowed so image generation always falls back to
    the text-only path rather than aborting.
    """
    from google.genai import types

    uri = (uri or "").strip()
    if not uri:
        return None
    try:
        if uri.startswith("gs://"):
            without_scheme = uri[len("gs://") :]
            bucket, _, obj = without_scheme.partition("/")
            if not bucket or not obj:
                logging.warning(f"Malformed gs:// reference image URI: '{uri}'")
                return None
            data = (download or _default_download)(bucket, obj)
            if len(data) > REFERENCE_MAX_BYTES:
                raise ValueError(f"larger than {REFERENCE_MAX_BYTES} bytes")
            mime = reference_mime_for(obj)
        elif uri.startswith("http://") or uri.startswith("https://"):
            data, mime = _fetch_http_reference(uri)
        else:
            logging.warning(
                f"Unsupported reference image URI scheme (want gs:// or http(s)://): '{uri}'"
            )
            return None
        return types.Part.from_bytes(data=data, mime_type=mime)
    except Exception as exc:
        logging.warning(
            f"Failed to fetch reference image '{uri}'; "
            f"falling back to text-only prompt: {exc}"
        )
        return None


async def fetch_references(
    refs: list[tuple[str, str]], download: Download | None = None
) -> tuple[list[tuple[str, Any]], list[str]]:
    """Fetch every reference concurrently (off the event loop).

    Returns ``(fetched, missing_roles)``: the ``(role, Part)`` pairs that
    fetched, in order, and the roles whose fetch failed
    (``fetch_reference_image`` returned None and logged a warning). A failed
    fetch drops only that reference.
    """
    parts = await asyncio.gather(
        *(
            asyncio.to_thread(fetch_reference_image, uri, download=download)
            for uri, _ in refs
        )
    )
    fetched = []
    missing = []
    for (uri, role), part in zip(refs, parts, strict=True):
        if part is None:
            logging.warning(f"Skipping {role} reference image '{uri}' (fetch failed)")
            missing.append(role)
            continue
        logging.info(f"Using {role} reference image: {uri}")
        fetched.append((role, part))
    return fetched, missing
