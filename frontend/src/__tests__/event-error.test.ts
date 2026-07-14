import { describe, it, expect } from "vitest";
import { getEventError } from "@/lib/api";
import type { AgentEvent } from "@/lib/types";

// An error event as emitted by the ADK api_server run_sse stream when a model
// call fails (e.g. Vertex 429). It carries errorCode/errorMessage (and often a
// separate final event with a top-level `error`), and has no `content`, so the
// run page's content-only loop used to drop it silently.

function makeEvent(overrides: Partial<AgentEvent>): AgentEvent {
  return {
    id: "e1",
    invocationId: "inv1",
    author: "root_agent",
    timestamp: 1,
    ...overrides,
  } as AgentEvent;
}

describe("getEventError", () => {
  it("returns null for a normal content event", () => {
    const ev = makeEvent({
      content: { role: "model", parts: [{ text: "hello" }] },
    });
    expect(getEventError(ev)).toBeNull();
  });

  it("returns null for a state-delta-only event", () => {
    const ev = makeEvent({ actions: { stateDelta: { brand: "PRS" } } });
    expect(getEventError(ev)).toBeNull();
  });

  it("detects errorCode + errorMessage events", () => {
    const ev = makeEvent({
      errorCode: "_SomeError",
      errorMessage: "something broke",
    });
    expect(getEventError(ev)).toBe("something broke");
  });

  it("falls back to errorCode when errorMessage is absent", () => {
    const ev = makeEvent({ errorCode: "_MalformedFunctionCall" });
    expect(getEventError(ev)).toBe("_MalformedFunctionCall");
  });

  it("detects a top-level error field (final error event)", () => {
    const ev = makeEvent({ id: "", error: "boom happened" } as Partial<AgentEvent>);
    expect(getEventError(ev)).toBe("boom happened");
  });

  it("gives a friendly message for a 429 RESOURCE_EXHAUSTED quota error", () => {
    const ev = makeEvent({
      errorCode: "_ResourceExhaustedError",
      errorMessage:
        '429 Too Many Requests. {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}',
    });
    const msg = getEventError(ev);
    expect(msg).toMatch(/quota/i);
    expect(msg).toMatch(/429/);
  });

  it("detects a 429 carried only in a top-level error string", () => {
    const ev = makeEvent({
      id: "",
      error: "_ResourceExhaustedError: 429 RESOURCE_EXHAUSTED",
    } as Partial<AgentEvent>);
    expect(getEventError(ev)).toMatch(/quota/i);
  });
});
