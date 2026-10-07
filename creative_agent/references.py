"""Reference images for image generation: which ones, in what order — pure.

Callers seed up to ``MAX_REFERENCE_IMAGES`` reference images via createSession
initialState as ``reference_images`` (a list of ``{"uri", "role"}`` objects, or
its JSON string). The legacy single-reference keys ``reference_image_uri`` +
``reference_image_role`` are folded in as the first entry. No SDK imports, so
both ``image_tools`` (rendering) and ``callbacks`` (state init) can use it.
"""

import json
from typing import Any

# Google allows up to 14 reference images; keep it small for render latency.
MAX_REFERENCE_IMAGES = 3

# How a reference image may be used. An empty role means `product` (the
# historical meaning of the single reference image).
REFERENCE_ROLES = ("product", "logo", "style")
_DEFAULT_ROLE = "product"


def _normalise_role(role: Any) -> str | None:
    """A valid role (empty → product), or None for an unknown/non-string role."""
    if role is None:
        return _DEFAULT_ROLE
    if not isinstance(role, str):
        return None
    role = role.strip().lower() or _DEFAULT_ROLE
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

    The legacy ``reference_image_uri`` (+ ``reference_image_role``) comes first,
    then the ``reference_images`` entries in order. Entries without a non-empty
    string ``uri`` or with an unknown role are skipped; a repeated uri keeps its
    first occurrence; at most ``MAX_REFERENCE_IMAGES`` are returned.
    """
    candidates: list[Any] = [
        {
            "uri": state.get("reference_image_uri"),
            "role": state.get("reference_image_role"),
        }
    ]
    candidates.extend(_listed_references(state.get("reference_images")))

    refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            continue
        uri = uri.strip()
        role = _normalise_role(entry.get("role"))
        if role is None or uri in seen:
            continue
        seen.add(uri)
        refs.append((uri, role))
        if len(refs) == MAX_REFERENCE_IMAGES:
            break
    return refs


def reference_roles_summary(state: Any) -> str:
    """The resolved references' roles in order, e.g. ``"product, style"``."""
    return ", ".join(role for _, role in resolve_references(state))
