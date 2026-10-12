import { describe, expect, it } from "vitest";
import { STEP_ERRORS_MAX, addStepError, stepErrorMessage } from "@/lib/step-errors";
import type { AgentEvent } from "@/lib/types";

const ev = (over: Partial<AgentEvent>): AgentEvent =>
  ({
    id: "e",
    invocationId: "i",
    author: "trend_situation_synthesizer",
    timestamp: 1,
    ...over,
  }) as AgentEvent;

describe("stepErrorMessage", () => {
  it("is null for a normal event", () => {
    expect(
      stepErrorMessage(ev({ content: { role: "model", parts: [{ text: "hi" }] } }))
    ).toBeNull();
  });

  it("returns the event's error message", () => {
    expect(
      stepErrorMessage(ev({ errorCode: "INVALID_ARGUMENT", errorMessage: "400 bad" }))
    ).toBe("400 bad");
  });

  it("keeps the friendly 429 mapping", () => {
    expect(stepErrorMessage(ev({ errorMessage: "429 RESOURCE_EXHAUSTED" }))).toMatch(
      /quota/i
    );
  });
});

describe("addStepError", () => {
  it("dedupes and preserves order", () => {
    expect(addStepError(addStepError(["a"], "b"), "a")).toEqual(["a", "b"]);
  });

  it("returns the same list when nothing changes", () => {
    const list = ["a"];
    expect(addStepError(list, "a")).toBe(list);
  });

  it("caps the list", () => {
    let list: string[] = [];
    for (let i = 0; i < STEP_ERRORS_MAX + 3; i++) list = addStepError(list, `m${i}`);
    expect(list).toHaveLength(STEP_ERRORS_MAX);
    expect(list[0]).toBe("m0");
  });
});
