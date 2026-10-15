"""Render ONE visual concept: references, optional person photo, QA + re-render.

``render_concept`` composes the ``image_tools`` helpers (reference prompt block,
quota-paced ``_render_image``, the person path with ``ALLOW_ADULT`` and its typed
safety fallback, and the bounded QA re-render ``_rerender_loop`` over a ``QAContext``) without
needing an ADK ``ToolContext``, so it serves both callers:

- ``image_tools.generate_image`` (the batch tool) calls it once per concept, as
  concurrent tasks that share one render semaphore (``IMAGE_RENDER_CONCURRENCY``
  image-model calls in flight) and the per-run re-render budget, chained so
  first renders are staggered by concept while each image's QA overlaps the
  other renders (``RenderPacing``);
- the api's personalised variants (``runserver.variants``) call it standalone
  with ``fallback_without_person=False`` (a variant is the person; a blocked
  photo is a ``rejected`` result, never a person-less render).

``render_edit`` is the variants' edit mode: instead of re-generating the scene it
edits the run's original render so only the person changes (``person_edit``),
and its image check also compares the edit with the original (composition).

It never touches session state, uploads or logs the person photo's URI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from google.genai import types

from . import image_qa, image_tools
from .concept_guard import neutralise_person_prompt
from .config import config
from .person_edit import EDIT_INSTRUCTION, edit_contents
from .person_render import NO_IMAGE
from .references import fetch_references as _fetch_reference_parts

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
    """Fetch ``resolve_references(state)`` output once, concurrently (gs://
    objects through ``image_tools._download_blob``, like the person photo)."""
    fetched, missing = await _fetch_reference_parts(
        refs, download=image_tools._download_blob
    )
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
    person_part: types.Part | None = None,
) -> Any:
    """The render contents — pure: the prompt + the no-other-logos line (always:
    an unrequested third-party logo was the top image-QA re-render cause), then
    the numbered reference block and the reference Parts (the person photo
    last, when given), or the bare prompt text when no Part is attached."""
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

    ``lock`` caps image-model calls in flight (a lock: one at a time; a
    semaphore: N, re-renders included); this concept's first render
    waits for ``start_after`` and sets ``rendered`` once its first render (incl.
    a person fallback) returned; the re-render budget is claimed after
    ``claim_after`` and ``claims_done`` is set once this concept claims no more.
    """

    lock: asyncio.Lock | asyncio.Semaphore | None = None
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


@dataclass(frozen=True)
class QAContext:
    """Everything one concept's image check needs besides the image itself.

    ``person_image`` (cast concepts only) adds the likeness comparison;
    ``original_image`` (personalised variant edits only) the composition check;
    ``safer_angle`` (a sensitive trend's safer angle) the public-figure likeness
    check; ``has_logo`` tells QA a logo reference was attached. ``model``
    defaults to ``config.image_qa_model``; ``client`` None = the shared lazily
    built QA client, resolved inside the fail-open check (so a client that
    can't be built keeps the render, like any other QA error).
    """

    brand: str = ""
    product: str = ""
    strictness: tuple[str, ...] = ()
    person_image: tuple[bytes, str] | None = None
    original_image: tuple[bytes, str] | None = None
    safer_angle: bool = False
    has_logo: bool = False
    model: str = field(default_factory=lambda: config.image_qa_model)
    client: Any = None


BUDGET_REACHED = "re-render budget reached"

# Re-renders the image with the quote-free correction text appended.
RenderFn = Callable[[str], Awaitable[tuple[bytes, str] | None]]


async def _inspect(
    rendered: tuple[bytes, str], entry: dict, qa_ctx: QAContext
) -> image_qa.ImageQAResult | None:
    """One QA call off the event loop → the verdict, or None (fail-open)."""
    image_bytes, mime = rendered
    extra: dict[str, Any] = {}
    if qa_ctx.person_image is not None:
        extra["person_image"] = qa_ctx.person_image
    if qa_ctx.original_image is not None:
        extra["original_image"] = qa_ctx.original_image
    if qa_ctx.safer_angle:
        extra["safer_angle_active"] = True
    try:
        client = qa_ctx.client
        if client is None:
            client = image_qa._get_qa_client()
        return await asyncio.to_thread(
            image_qa.inspect_image,
            image_bytes,
            mime,
            entry,
            brand=qa_ctx.brand,
            target_product=qa_ctx.product,
            client=client,
            model=qa_ctx.model,
            has_logo_reference=qa_ctx.has_logo,
            strictness=qa_ctx.strictness,
            **extra,
        )
    except Exception as exc:
        logging.warning(
            f"Image QA unavailable for '{entry.get('concept_name')}': {exc}"
        )
        return None


async def _rerender_loop(
    qa_ctx: QAContext,
    render_fn: RenderFn,
    budget: image_tools._RerenderBudget,
    *,
    entry: dict,
    first: tuple[bytes, str],
    claim_after: asyncio.Event | None = None,
    claims_done: asyncio.Event | None = None,
) -> tuple[tuple[bytes, str], int, dict | None, str | None]:
    """Inspect ``first``; re-render (bounded) while it fails; keep the best.

    Returns ``(kept_image, attempts, qa_record, issue)``. Each re-render is
    ``render_fn(image_qa.correction_text(...))`` (the caller appends the
    quote-free, "no new text" correction to its prompt or edit instruction) and
    is inspected again; ``image_tools._keep_retry`` decides which attempt is
    kept. Re-renders are bounded per image (``config.image_qa_max_rerenders``)
    and per run (``budget``, spent here). ``issue`` is the ``image_qa__issues``
    entry: "<concept>: <failures>" when the kept image still fails (suffixed
    "(re-render budget reached)" when the run cap stopped a re-render), else
    None. ``qa_record`` is None when the first check errored (fail-open; the
    render is kept and the caller records the concept as unavailable). A
    re-render that errors or returns no image, or a re-check that errors, stops
    the loop and keeps the best inspected image so far.

    Pipelining (``generate_image``): the budget is claimed in concept order —
    this concept decides only after the previous one set ``claim_after`` — and
    ``claims_done`` is set once this concept will claim no more (always set on
    exit, so a failure never blocks later concepts).
    """
    product = qa_ctx.product

    async def my_turn() -> None:
        # Wait until every earlier concept has made its budget claims, so the
        # per-run cap goes to concepts in concept order (as when sequential).
        if claim_after is not None:
            await claim_after.wait()

    try:
        name = entry.get("concept_name", "")
        result = await _inspect(first, entry, qa_ctx)
        if result is None:
            await my_turn()
            return first, 1, None, None
        attempts = 1
        kept, kept_result = first, result
        kept_rules = image_qa.qa_failed_rules(result, entry, target_product=product)
        budget_reached = False
        max_rerenders = config.image_qa_max_rerenders
        for round_ in range(max_rerenders):
            if not kept_rules:
                break
            await my_turn()
            if budget.remaining <= 0:
                budget_reached = True
                break
            budget.remaining -= 1
            if round_ == max_rerenders - 1 and claims_done is not None:
                claims_done.set()  # last possible claim made: let the next decide
            logging.warning(
                f"Image QA failed for '{name}' ({kept_rules}); re-rendering"
            )
            attempts += 1
            correction = image_qa.correction_text(
                kept_result, entry, target_product=product
            )
            try:
                retry = await render_fn(correction)
            except Exception as exc:
                logging.warning(
                    f"Re-render failed for '{name}'; keeping previous: {exc}"
                )
                break
            if retry is None:
                break
            retry_result = await _inspect(retry, entry, qa_ctx)
            if retry_result is None:
                break
            retry_rules = image_qa.qa_failed_rules(
                retry_result, entry, target_product=product
            )
            if image_tools._keep_retry(kept_rules, retry_rules):
                kept, kept_result, kept_rules = retry, retry_result, retry_rules
        await my_turn()
        record = image_tools._qa_record(
            kept_result,
            entry,
            product,
            compared_to_original=qa_ctx.original_image is not None,
            safer_angle_active=qa_ctx.safer_angle,
        )
        issue = None
        if record["failures"]:
            issue = f"{name}: {'; '.join(record['failures'])}"
            if budget_reached:
                issue += f" ({BUDGET_REACHED})"
        return kept, attempts, record, issue
    finally:
        if claims_done is not None:
            claims_done.set()


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
    safer_angle_active: bool = False,
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
    ``safer_angle_active`` (a sensitive trend's safer angle) adds image QA's
    public-figure likeness check.
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

        async def rerender(correction: str) -> tuple[bytes, str] | None:
            contents = contents_for(prompt_text + "\n\n" + correction, cast)
            async with lock:
                if not cast:
                    return await image_tools._render_image(contents, aspect_ratio)
                # A cast concept's re-render keeps the person part and config;
                # a blocked re-render yields None (the previous one is kept).
                retry, _reason = await image_tools._render_person_image(
                    contents, aspect_ratio
                )
                return retry

        qa_ctx = QAContext(
            brand=brand,
            product=target_product,
            strictness=tuple(strictness),
            person_image=person_image_of(person) if cast else None,
            safer_angle=safer_angle_active,
            has_logo=references.has_logo,
        )
        kept, attempts, record, issue = await _rerender_loop(
            qa_ctx,
            rerender,
            budget,
            entry=entry,
            first=rendered,
            claim_after=pacing.claim_after,
            claims_done=pacing.claims_done,
        )
        return _result(kept, attempts, record, issue, cast, rejected, entry, True)
    finally:
        if pacing.claims_done is not None:
            pacing.claims_done.set()


async def render_edit(
    base_image: tuple[bytes, str],
    person: types.Part,
    concept: dict,
    *,
    aspect_ratio: str,
    strictness: Sequence[str] = (),
    qa: bool = True,
    brand: str = "",
    target_product: str = "",
    budget: image_tools._RerenderBudget | None = None,
) -> RenderResult:
    """Edit the original render ``base_image`` (``(bytes, mime)``) so only its
    main person becomes the person in ``person`` (personalised variants).

    One edit from the original (``person_edit.edit_contents``: the instruction,
    the labelled original, the labelled person photo) with
    ``person_generation=ALLOW_ADULT``. A blocked edit returns no image and the
    ``rejected_reason``; there is no fallback. With ``qa``, the image check
    compares the edit with the person photo (likeness) and the original
    (composition); a failure repeats the edit of the original with
    ``image_qa.correction_text`` appended to the instruction (bounded like
    ``render_concept``). ``concept`` is QA context only (motif, product, text);
    its prompt is never sent to the image model.
    """
    budget = budget or image_tools._RerenderBudget(config.image_qa_max_rerenders)
    entry = {**concept, "casts_person_reference": True}
    base_part = types.Part.from_bytes(data=base_image[0], mime_type=base_image[1])

    def contents_for(instruction: str) -> list[types.Part]:
        return edit_contents(
            base_part,
            person,
            brand=brand,
            strictness=strictness,
            instruction=instruction,
        )

    rendered, reason = await image_tools._render_person_image(
        contents_for(EDIT_INSTRUCTION), aspect_ratio
    )
    if rendered is None:
        logging.warning(
            f"Person edit for '{entry.get('concept_name', '')}' rejected ({reason})"
        )
        return RenderResult(
            None, "", 1, None, None, False, reason or NO_IMAGE, concept=entry
        )
    if not qa:
        return _result(rendered, 1, None, None, True, None, entry, False)

    async def rerender(correction: str) -> tuple[bytes, str] | None:
        retry, _reason = await image_tools._render_person_image(
            contents_for(EDIT_INSTRUCTION + "\n\n" + correction), aspect_ratio
        )
        return retry  # a blocked re-edit keeps the previous attempt

    qa_ctx = QAContext(
        brand=brand,
        product=target_product,
        strictness=tuple(strictness),
        person_image=person_image_of(person),
        original_image=base_image,
    )
    kept, attempts, record, issue = await _rerender_loop(
        qa_ctx, rerender, budget, entry=entry, first=rendered
    )
    return _result(kept, attempts, record, issue, True, None, entry, True)


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
