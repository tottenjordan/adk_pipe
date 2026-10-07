"""Reference images for image generation: which ones, in what order — pure.

Callers seed up to ``MAX_REFERENCE_IMAGES`` reference images via createSession
initialState as ``reference_images`` (a list of ``{"uri", "role"}`` objects, or
its JSON string). The legacy single-reference keys ``reference_image_uri`` +
``reference_image_role`` are folded in as the first entry. No SDK imports, so
both ``image_tools`` (rendering) and ``callbacks`` (state init) can use it.
"""

import json
import logging
from typing import Any

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
