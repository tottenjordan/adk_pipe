"""Render ONE visual concept: references, optional person photo, QA + re-render.

``render_concept`` composes the ``image_tools`` helpers (reference prompt block,
quota-paced ``_render_image``, the person path with ``ALLOW_ADULT`` and its typed
safety fallback, and the bounded QA re-render ``_inspect_and_rerender``) without
needing an ADK ``ToolContext``, so it serves both callers:

- ``image_tools.generate_image`` (the batch tool) calls it once per concept, as
  concurrent tasks that share one render lock and the per-run re-render budget,
  chained so renders stay strictly sequential and in concept order while each
  image's QA overlaps the next render (``RenderPacing``);
- the api's personalised variants (``runserver.variants``) call it standalone
  with ``fallback_without_person=False`` (a variant is the person; a blocked
  photo is a ``rejected`` result, never a person-less render).

It never touches session state, uploads or logs the person photo's URI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from . import image_tools
from .concept_guard import neutralise_person_prompt
from .config import config
from .person_render import NO_IMAGE

PHOTO_UNAVAILABLE = "photo_unavailable"


@dataclass(frozen=True)
class RenderReferences:
    """The run's fetched reference images (product / logo / style)."""

    parts: tuple[tuple[str, types.Part], ...] = ()
    missing_roles: tuple[str, ...] = ()

    @property
    def roles(self) -> list[str]:
        return [role for role, _ in self.parts]

    @property
    def has_logo(self) -> bool:
        return "logo" in self.roles


async def fetch_references(refs: list[tuple[str, str]]) -> RenderReferences:
    """Fetch ``resolve_references(state)`` output once, concurrently."""
    fetched, missing = await image_tools._fetch_references(refs)
    return RenderReferences(parts=tuple(fetched), missing_roles=tuple(missing))


async def fetch_person_photo(uri: str) -> types.Part | None:
    """The consented person photo as a Part (off the event loop), or None."""
    return await asyncio.to_thread(image_tools._fetch_person_photo, uri)


def person_image_of(part: types.Part | None) -> tuple[bytes, str] | None:
    """``(bytes, mime)`` of a fetched person photo, for the QA likeness check."""
    inline = getattr(part, "inline_data", None)
    if inline is not None and inline.data:
        return inline.data, inline.mime_type or "image/jpeg"
    return None


def render_contents(
    prompt_text: str,
    references: RenderReferences,
    *,
    brand: str = "",
    strictness: Sequence[str] = (),
    person_part: types.Part | None = None,
) -> Any:
    """The render contents — pure: the prompt (+ the ``unwanted_logo``
    rating-strictness line, the numbered reference block and the reference
    Parts; the person photo last, when given), or the bare prompt text when no
    Part is attached."""
    if "unwanted_logo" in strictness:
        prompt_text = prompt_text + "\n\n" + image_tools.no_other_logos_line(brand)
    roles = references.roles
    parts = [part for _, part in references.parts]
    if person_part is not None:
        roles.append("person")
        parts.append(person_part)
    if parts:
        return [
            image_tools._reference_prompt(
                prompt_text, roles, list(references.missing_roles)
            ),
            *parts,
        ]
    return prompt_text


@dataclass
class RenderPacing:
    """Pipelining hooks for batch renders (all optional; standalone = none).

    ``lock`` serialises every image-model call; this concept's first render
    waits for ``start_after`` and sets ``rendered`` once its first render (incl.
    a person fallback) returned; the re-render budget is claimed after
    ``claim_after`` and ``claims_done`` is set once this concept claims no more.
    """

    lock: asyncio.Lock | None = None
    start_after: asyncio.Event | None = None
    rendered: asyncio.Event | None = None
    claim_after: asyncio.Event | None = None
    claims_done: asyncio.Event | None = None


@dataclass
class RenderResult:
    """One concept's outcome. ``concept`` is the concept as rendered (a person
    fallback clears its cast and neutralises its prompt); ``cast`` is whether
    the kept image was rendered with the person photo; ``rejected_reason`` is
    why a requested person render didn't happen (``photo_unavailable`` or the
    block reason); ``qa_unavailable`` means the check errored (fail-open)."""

    image_bytes: bytes | None
    mime: str
    attempts: int
    qa: dict | None
    qa_issue: str | None
    cast: bool
    rejected_reason: str | None
    concept: dict = field(default_factory=dict)
    qa_unavailable: bool = False

    @property
    def rendered(self) -> tuple[bytes, str] | None:
        if self.image_bytes is None:
            return None
        return self.image_bytes, self.mime


async def render_concept(
    concept: dict,
    *,
    aspect_ratio: str,
    references: RenderReferences | None = None,
    person: types.Part | None = None,
    strictness: Sequence[str] = (),
    qa: bool = True,
    brand: str = "",
    target_product: str = "",
    budget: image_tools._RerenderBudget | None = None,
    fallback_without_person: bool = True,
    pacing: RenderPacing | None = None,
) -> RenderResult:
    """Render ``concept`` (its ``image_generation_prompt``) once, then QA it and
    re-render while it fails (bounded by ``config.image_qa_max_rerenders`` and
    ``budget``; default: one image's worth), keeping the better attempt.

    A concept with ``casts_person_reference is True`` gets ``person`` (the
    fetched photo Part) attached as the last reference with
    ``person_generation=ALLOW_ADULT``. When the photo is missing or a safety
    filter blocks the render, ``fallback_without_person`` re-renders a generic
    hero (prompt neutralised, judged as uncast); otherwise the result carries no
    image and the ``rejected_reason``. Infra errors on a first render propagate.
    """
    references = references or RenderReferences()
    pacing = pacing or RenderPacing()
    budget = budget or image_tools._RerenderBudget(config.image_qa_max_rerenders)
    lock = pacing.lock or contextlib.nullcontext()
    entry = dict(concept)
    name = str(entry.get("concept_name") or "")
    prompt_text = entry["image_generation_prompt"]
    wants_person = entry.get("casts_person_reference") is True
    cast = wants_person
    rejected: str | None = None
    rendered: tuple[bytes, str] | None = None

    def contents_for(text: str, with_person: bool = False) -> Any:
        return render_contents(
            text,
            references,
            brand=brand,
            strictness=strictness,
            person_part=person if with_person else None,
        )

    try:
        if pacing.start_after is not None:
            await pacing.start_after.wait()
        if cast and person is None:
            rejected, cast = PHOTO_UNAVAILABLE, False
        elif cast:
            async with lock:
                rendered, reason = await image_tools._render_person_image(
                    contents_for(prompt_text, True), aspect_ratio
                )
            if rendered is None:
                logging.warning(
                    f"Person render for '{name}' rejected ({reason}); "
                    + (
                        "rendering without the person"
                        if fallback_without_person
                        else "no fallback"
                    )
                )
                rejected, cast = reason or NO_IMAGE, False
        if wants_person and not cast:
            if not fallback_without_person:
                if pacing.rendered is not None:
                    pacing.rendered.set()
                return RenderResult(
                    None, "", 1, None, None, False, rejected, concept=entry
                )
            # The typed fallback: a generic hero, no person part (and QA judges
            # it as an uncast concept).
            prompt_text = neutralise_person_prompt(prompt_text)
            entry = {
                **entry,
                "casts_person_reference": False,
                "image_generation_prompt": prompt_text,
            }
        if rendered is None:
            async with lock:
                rendered = await image_tools._render_image(
                    contents_for(prompt_text), aspect_ratio
                )
        if pacing.rendered is not None:
            pacing.rendered.set()

        if rendered is None or not qa:
            if pacing.claim_after is not None:
                await pacing.claim_after.wait()  # keep the concept-order chain
            return _result(rendered, 1, None, None, cast, rejected, entry, False)

        async def rerender(contents, ratio: str) -> tuple[bytes, str] | None:
            async with lock:
                if not cast:
                    return await image_tools._render_image(contents, ratio)
                # A cast concept's re-render keeps the person part and config;
                # a blocked re-render yields None (the previous one is kept).
                retry, _reason = await image_tools._render_person_image(contents, ratio)
                return retry

        kept, attempts, record, issue = await image_tools._inspect_and_rerender(
            entry,
            rendered,
            prompt_text,
            aspect_ratio,
            lambda text: contents_for(text, cast),
            brand,
            target_product,
            budget,
            references.has_logo,
            render=rerender,
            claim_after=pacing.claim_after,
            claims_done=pacing.claims_done,
            strictness=strictness,
            person_image=person_image_of(person) if cast else None,
        )
        return _result(kept, attempts, record, issue, cast, rejected, entry, True)
    finally:
        if pacing.claims_done is not None:
            pacing.claims_done.set()


def _result(
    rendered: tuple[bytes, str] | None,
    attempts: int,
    record: dict | None,
    issue: str | None,
    cast: bool,
    rejected: str | None,
    entry: dict,
    qa_ran: bool,
) -> RenderResult:
    image_bytes, mime = rendered if rendered is not None else (None, "")
    return RenderResult(
        image_bytes,
        mime,
        attempts,
        record,
        issue,
        cast,
        rejected,
        concept=entry,
        qa_unavailable=qa_ran and rendered is not None and record is None,
    )
