"""generate_image pipelines image QA with the next render.

Renders stay strictly sequential (the 2-RPM image quota), but image N's QA call
runs while image N+1 renders, and a re-render for N is queued right after the
render in flight (ahead of the next first render). Outcomes are unchanged:
the same ``generated_images`` records, issues and uploads (kept image only, in
concept order), and the per-run re-render budget still goes to concepts in
concept order whatever order their QA calls finish in.
"""

import threading
import time
from collections.abc import Callable

import pytest

from creative_agent import image_qa, image_tools
from tests.test_image_qa import _CONCEPT, _Flow, _ImgResponse, _result

_WAIT = 5.0  # bounded waits: a non-pipelined implementation fails, never hangs


def _concepts(n: int) -> list[dict]:
    return [
        {
            **_CONCEPT,
            "concept_name": f"C{i}",
            "image_generation_prompt": f"PROMPT-{i} a guitar beside a lottery ball",
        }
        for i in range(1, n + 1)
    ]


def _concept_of(contents) -> str:
    text = contents if isinstance(contents, str) else contents[0]
    return "C" + text.split("PROMPT-", 1)[1].split(" ", 1)[0]


class _Pipeline:
    """A _Flow whose renders and QA calls log events and can be gated."""

    def __init__(self, monkeypatch, n, verdicts, **flow_kwargs):
        self.flow = _Flow(monkeypatch, [], concepts=_concepts(n), **flow_kwargs)
        self.events: list[str] = []
        self.lock = threading.Lock()
        self.qa_started: dict[str, threading.Event] = {}
        self.qa_done: dict[str, threading.Event] = {}
        self.render_gates: dict[int, Callable[[], object]] = {}
        self.qa_delays: dict[tuple[str, int], float] = {}
        self.qa_gates: dict[str, Callable[[], object]] = {}
        self.render_started: dict[int, threading.Event] = {
            i: threading.Event() for i in range(1, 2 * n + 1)
        }
        self.verdicts = {k: list(v) for k, v in verdicts.items()}
        self.renders: list[str] = []
        self.qa_calls: dict[str, int] = {}
        for i in range(1, n + 1):
            self.qa_started[f"C{i}"] = threading.Event()
            self.qa_done[f"C{i}"] = threading.Event()

        def log(event: str) -> None:
            with self.lock:
                self.events.append(event)

        def render(**kwargs):
            with self.lock:
                self.renders.append(_concept_of(kwargs["contents"]))
                index = len(self.renders)
            log(f"render_start:{index}")
            self.render_started[index].set()
            gate = self.render_gates.get(index)
            if gate is not None:
                gate()
            log(f"render_done:{index}")
            return _ImgResponse(f"img{index}".encode())

        def inspect(image_bytes, mime, concept, **_kw):
            name = concept["concept_name"]
            with self.lock:
                self.qa_calls[name] = self.qa_calls.get(name, 0) + 1
                call = self.qa_calls[name]
            log(f"qa_start:{name}")
            self.qa_started[name].set()
            time.sleep(self.qa_delays.get((name, call), 0))
            gate = self.qa_gates.get(name)
            if gate is not None and call == 1:
                gate()
            verdict = self.verdicts[name].pop(0)
            log(f"qa_done:{name}")
            self.qa_done[name].set()
            return verdict

        monkeypatch.setattr(self.flow.models, "generate_content", render)
        monkeypatch.setattr(image_qa, "inspect_image", inspect)

    def run(self):
        return self.flow.run()

    @property
    def state(self):
        return self.flow.ctx.state

    def index(self, event: str) -> int:
        return self.events.index(event)


def test_qa_of_image_1_overlaps_render_of_image_2(monkeypatch):
    p = _Pipeline(monkeypatch, 2, {"C1": [_result()], "C2": [_result()]})
    # QA 1 completes only once render 2 has started, and render 2 completes
    # only once QA 1 has started (bounded waits): they must run concurrently.
    p.qa_gates["C1"] = lambda: p.render_started[2].wait(_WAIT)
    p.render_gates[2] = lambda: p.qa_started["C1"].wait(_WAIT)

    p.run()

    assert p.index("qa_start:C1") < p.index("render_done:2")
    assert p.index("render_start:2") < p.index("qa_done:C1")
    # Renders never overlap each other.
    assert p.index("render_done:1") < p.index("render_start:2")
    keys = [image_tools.artifact_key_for(f"C{i}") for i in (1, 2)]
    assert p.flow.uploads == [(keys[0], b"img1"), (keys[1], b"img2")]
    assert list(p.state["generated_images"]) == ["C1", "C2"]
    assert [r["attempts"] for r in p.state["generated_images"].values()] == [1, 1]


def test_rerender_queued_right_after_the_render_in_flight(monkeypatch):
    bad = _result(artifacts=True, issues=["warped neck"])
    p = _Pipeline(
        monkeypatch,
        3,
        {"C1": [bad, _result()], "C2": [_result()], "C3": [_result()]},
    )

    def hold_render_2():
        # Keep render 2 in flight until C1's failed QA has queued its re-render.
        p.qa_done["C1"].wait(_WAIT)
        time.sleep(0.2)

    p.render_gates[2] = hold_render_2

    p.run()

    # C1's re-render jumps ahead of C3's first render.
    assert p.renders == ["C1", "C2", "C1", "C3"]
    images = p.state["generated_images"]
    assert list(images) == ["C1", "C2", "C3"]
    assert images["C1"]["attempts"] == 2
    keys = {f"C{i}": image_tools.artifact_key_for(f"C{i}") for i in (1, 2, 3)}
    # Only the kept (re-rendered) C1 image is uploaded; concept order kept.
    assert p.flow.uploads == [
        (keys["C1"], b"img3"),
        (keys["C2"], b"img2"),
        (keys["C3"], b"img4"),
    ]
    assert "image_qa__issues" not in p.state


@pytest.mark.parametrize("pipelined_qa_order", ["in_order", "c1_slow"])
def test_budget_goes_to_concepts_in_concept_order(monkeypatch, pipelined_qa_order):
    """per_run=1: C1 gets the re-render even when C2's QA finishes first."""
    bad = _result(artifacts=True)
    p = _Pipeline(
        monkeypatch,
        2,
        {"C1": [bad, _result()], "C2": [bad]},
        per_run=1,
    )
    if pipelined_qa_order == "c1_slow":
        p.qa_delays[("C1", 1)] = 0.3

    p.run()

    images = p.state["generated_images"]
    assert images["C1"]["attempts"] == 2
    assert images["C2"]["attempts"] == 1
    assert p.state["image_qa__issues"] == [
        "C2: severe visual artifacts (re-render budget reached)"
    ]


def test_outcomes_unchanged_for_mixed_pass_fail_rerender(monkeypatch):
    """pass / fail→pass / fail→fail (kept first) / QA error, in one call."""
    p = _Pipeline(
        monkeypatch,
        4,
        {
            "C1": [_result()],
            "C2": [_result(unrequested_logos=True), _result()],
            "C3": [
                _result(unrequested_logos=True),
                _result(unrequested_logos=True, artifacts=True),
            ],
            "C4": [],  # QA raises -> fail-open
        },
    )

    def broken_inspect(image_bytes, mime, concept, **kw):
        if concept["concept_name"] == "C4":
            raise RuntimeError("vision down")
        return original(image_bytes, mime, concept, **kw)

    original = image_qa.inspect_image
    monkeypatch.setattr(image_qa, "inspect_image", broken_inspect)

    p.run()

    images = p.state["generated_images"]
    assert list(images) == ["C1", "C2", "C3", "C4"]
    assert [images[c]["attempts"] for c in images] == [1, 2, 2, 1]
    assert images["C1"]["qa"]["passed"] is True
    assert images["C2"]["qa"]["passed"] is True
    assert images["C3"]["qa"]["failures"] == ["unrequested third-party logo"]
    assert images["C4"]["qa"] is None
    assert p.state["image_qa__issues"] == ["C3: unrequested third-party logo"]
    assert p.state["image_qa__unavailable"] == ["C4"]
    # One upload per concept (the kept attempt), in concept order.
    uploaded = [key for key, _ in p.flow.uploads]
    assert uploaded == [image_tools.artifact_key_for(c) for c in images]
    assert len(p.renders) == 6
    by_concept = {}
    for index, concept in enumerate(p.renders, start=1):
        by_concept.setdefault(concept, []).append(f"img{index}".encode())
    kept = dict(p.flow.uploads)
    assert kept[image_tools.artifact_key_for("C2")] == by_concept["C2"][1]
    assert kept[image_tools.artifact_key_for("C3")] == by_concept["C3"][0]
    assert p.state["_images_generated"] is True


def test_render_failure_raises_and_cancels_pending_qa(monkeypatch):
    """A failed render still propagates (ADK RetryConfig) while QA 1 is pending;
    nothing is marked generated."""
    p = _Pipeline(monkeypatch, 2, {"C1": [_result()], "C2": [_result()]})
    p.qa_delays[("C1", 1)] = 0.3

    def render_2_fails():
        raise ValueError("bad request")

    p.render_gates[2] = render_2_fails

    with pytest.raises(ValueError, match="bad request"):
        p.run()

    assert "_images_generated" not in p.state
    assert "generated_images" not in p.state
    assert p.flow.uploads == []


def test_pipelining_is_inert_with_qa_disabled(monkeypatch):
    p = _Pipeline(monkeypatch, 3, {}, enabled=False)
    p.run()
    assert p.renders == ["C1", "C2", "C3"]
    assert [r["attempts"] for r in p.state["generated_images"].values()] == [1, 1, 1]
    assert p.qa_calls == {}
