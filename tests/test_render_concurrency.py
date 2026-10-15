"""generate_image keeps at most IMAGE_RENDER_CONCURRENCY image-model calls in flight.

The image-model calls are replaced by async fakes whose completion the test
releases one by one (``asyncio.Event`` gates), so the in-flight count and the
start order are observed exactly: first renders start in concept order, concept
i waits for concept i-N's first render, and QA re-renders share the same cap.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from creative_agent import image_qa, image_tools
from tests._fakes import FakeToolContext
from tests.test_image_qa import _CONCEPT, _result

_WAIT = 5.0  # bounded polling: a wrong implementation fails, never hangs


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


class _GatedRenders:
    """Fake ``_render_image``: each call blocks until the test releases it."""

    def __init__(self, fail: set[int] | None = None):
        self.starts: list[str] = []
        self.finished: list[str] = []
        self.gates: list[asyncio.Event] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail = fail or set()

    async def render(self, contents, aspect_ratio, image_config=None):
        index = len(self.starts)
        self.starts.append(_concept_of(contents))
        gate = asyncio.Event()
        self.gates.append(gate)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await gate.wait()
        finally:
            self.in_flight -= 1
        if index in self.fail:
            raise ValueError("bad request")
        self.finished.append(self.starts[index])
        return f"img{index + 1}".encode(), "image/png"

    def release(self, index: int) -> None:
        self.gates[index].set()


class _Run:
    """generate_image with gated renders, scripted QA and recorded uploads."""

    def __init__(self, monkeypatch, n, *, concurrency, verdicts=None, fail=None):
        self.renders = _GatedRenders(fail)
        self.uploads: list[tuple[str, bytes]] = []
        self.qa_calls: list[str] = []
        # Set to hold a concept's first check (in its worker thread).
        self.qa_gates: dict[str, threading.Event] = {}
        verdicts = {k: list(v) for k, v in (verdicts or {}).items()}
        monkeypatch.setattr(image_tools, "_render_image", self.renders.render)

        def fake_save(*, tool_context, image_bytes, filename, metadata=None):
            self.uploads.append((filename, image_bytes))
            return f"gs://b/{filename}"

        def inspect(image_bytes, mime, concept, **_kw):
            name = concept["concept_name"]
            gate = self.qa_gates.get(name)
            if gate is not None and name not in self.qa_calls:
                assert gate.wait(_WAIT), f"{name}'s check never released"
            self.qa_calls.append(name)
            queue = verdicts.get(name) or []
            return queue.pop(0) if queue else _result()

        monkeypatch.setattr(image_tools, "_save_to_gcs", fake_save)
        monkeypatch.setattr(image_tools.config, "image_render_concurrency", concurrency)
        monkeypatch.setattr(image_tools.config, "image_qa_enabled", bool(verdicts))
        monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders", 1)
        monkeypatch.setattr(image_tools.config, "image_qa_max_rerenders_per_run", 8)
        monkeypatch.setattr(image_tools.config, "image_qa_model", "qa-model")
        monkeypatch.setattr(image_qa, "_get_qa_client", lambda: "qa-client")
        monkeypatch.setattr(image_qa, "inspect_image", inspect)
        self.ctx = FakeToolContext(
            {
                "gcs_folder": "f",
                "agent_output_dir": "d",
                "brand": "PRS",
                "target_product": "guitar",
                "final_visual_concepts": {"visual_concepts": _concepts(n)},
            }
        )

    def start(self) -> asyncio.Task:
        return asyncio.create_task(image_tools.generate_image(self.ctx))


async def _until(condition, what: str) -> None:
    deadline = time.monotonic() + _WAIT
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def _settle() -> None:
    """Let every runnable task (and QA threads) make progress."""
    await asyncio.sleep(0.1)


def test_two_renders_in_flight_and_concept_i_waits_for_i_minus_n(monkeypatch):
    run = _Run(monkeypatch, 4, concurrency=2)
    r = run.renders

    async def scenario():
        task = run.start()
        await _until(lambda: len(r.starts) == 2, "two first renders")
        await _settle()
        # C1 and C2 both started before either finished; C3 is not let in.
        assert r.starts == ["C1", "C2"]
        assert r.finished == []
        r.release(1)  # C2 returns first: C4 (waits for C2) may go, C3 may not
        await _until(lambda: len(r.starts) == 3, "C4's first render")
        await _settle()
        assert r.starts == ["C1", "C2", "C4"]
        r.release(0)  # C1 returns: C3 (concept 3 waits for concept 1) starts
        await _until(lambda: len(r.starts) == 4, "C3's first render")
        assert r.starts == ["C1", "C2", "C4", "C3"]
        r.release(2)
        r.release(3)
        return await asyncio.wait_for(task, _WAIT)

    result = asyncio.run(scenario())

    assert result["status"] == "success"
    assert r.max_in_flight == 2
    keys = [image_tools.artifact_key_for(f"C{i}") for i in range(1, 5)]
    assert [key for key, _ in run.uploads] == keys  # concept order, after renders
    assert list(run.ctx.state["generated_images"]) == ["C1", "C2", "C3", "C4"]


def test_concurrency_one_is_strictly_sequential(monkeypatch):
    run = _Run(monkeypatch, 3, concurrency=1)
    r = run.renders

    async def scenario():
        task = run.start()
        for index in range(3):
            await _until(lambda i=index: len(r.starts) == i + 1, f"render {index + 1}")
            await _settle()
            assert len(r.starts) == index + 1  # the next never starts early
            r.release(index)
        return await asyncio.wait_for(task, _WAIT)

    asyncio.run(scenario())

    assert r.starts == ["C1", "C2", "C3"]
    assert r.finished == ["C1", "C2", "C3"]
    assert r.max_in_flight == 1


def test_rerenders_count_toward_the_cap(monkeypatch):
    bad = _result(artifacts=True, issues=["warped neck"])
    run = _Run(
        monkeypatch,
        3,
        concurrency=2,
        verdicts={"C1": [bad, _result()], "C2": [_result()], "C3": [_result()]},
    )
    r = run.renders
    # C1's check (slow in production) finishes only once C3 has its slot.
    run.qa_gates["C1"] = threading.Event()

    async def scenario():
        task = run.start()
        await _until(lambda: len(r.starts) == 2, "two first renders")
        r.release(0)  # C1 renders; C3 takes the free slot
        await _until(lambda: len(r.starts) == 3, "C3's first render")
        run.qa_gates["C1"].set()  # C1's check fails and queues a re-render
        await _until(lambda: run.qa_calls.count("C1") == 1, "C1's first check")
        await _settle()
        # C2 and C3 hold both slots: C1's re-render waits.
        assert r.starts == ["C1", "C2", "C3"]
        assert r.in_flight == 2
        r.release(1)
        await _until(lambda: len(r.starts) == 4, "C1's re-render")
        assert r.starts[3] == "C1"
        r.release(2)
        r.release(3)
        return await asyncio.wait_for(task, _WAIT)

    asyncio.run(scenario())

    assert r.max_in_flight == 2
    images = run.ctx.state["generated_images"]
    assert images["C1"]["attempts"] == 2
    assert dict(run.uploads)[image_tools.artifact_key_for("C1")] == b"img4"


@pytest.mark.parametrize("failing", [0, 1])
def test_first_render_failure_aborts_with_nothing_uploaded(monkeypatch, failing):
    run = _Run(monkeypatch, 4, concurrency=2, fail={failing})
    r = run.renders

    async def scenario():
        task = run.start()
        await _until(lambda: len(r.starts) == 2, "two first renders")
        r.release(failing)
        with pytest.raises(ValueError, match="bad request"):
            await asyncio.wait_for(task, _WAIT)

    asyncio.run(scenario())

    assert run.uploads == []
    assert "generated_images" not in run.ctx.state
    assert "_images_generated" not in run.ctx.state


def test_next_concepts_rerender_starts_before_this_rerender_returns(monkeypatch):
    """The budget is claimed in concept order, but once concept i has made its last
    possible claim it releases ``claims_done`` before re-rendering, so concept
    i+1's re-render can start while concept i's re-render is still in flight."""
    bad = _result(artifacts=True, issues=["warped neck"])
    run = _Run(
        monkeypatch,
        2,
        concurrency=2,
        verdicts={"C1": [bad, _result()], "C2": [bad, _result()]},
    )
    r = run.renders

    async def scenario():
        task = run.start()
        await _until(lambda: len(r.starts) == 2, "two first renders")
        r.release(0)
        await _until(lambda: len(r.starts) == 3, "C1's re-render")
        assert r.starts[2] == "C1"
        r.release(1)  # C2 renders, fails its check and wants a re-render
        # C1's re-render (index 2) is still held: C2's must start anyway.
        await _until(lambda: len(r.starts) == 4, "C2's re-render")
        assert r.starts[3] == "C2"
        r.release(2)
        r.release(3)
        return await asyncio.wait_for(task, _WAIT)

    result = asyncio.run(scenario())

    assert result["status"] == "success"
    images = run.ctx.state["generated_images"]
    assert images["C1"]["attempts"] == 2 and images["C2"]["attempts"] == 2
