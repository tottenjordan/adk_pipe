import { describe, it, expect } from "vitest";
import type { AgentEvent } from "@/lib/types";

// Test pause detection logic used in the run page

function detectPause(event: AgentEvent): {
  functionCallId: string;
  functionName: string;
  eventId: string;
} | null {
  if (!event.longRunningToolIds || event.longRunningToolIds.length === 0) {
    return null;
  }

  const functionCalls =
    event.content?.parts
      ?.filter((p) => p.functionCall)
      ?.map((p) => p.functionCall!) ?? [];

  const pausedCall = functionCalls.find((fc) =>
    event.longRunningToolIds!.includes(fc.id ?? "")
  );

  if (!pausedCall) return null;

  return {
    functionCallId: pausedCall.id ?? "",
    functionName: pausedCall.name ?? "",
    eventId: event.id,
  };
}

describe("Interactive mode pause detection", () => {
  it("detects a long-running tool pause event", () => {
    const event: AgentEvent = {
      id: "evt-1",
      invocationId: "inv-1",
      author: "root_agent",
      content: {
        role: "assistant",
        parts: [
          {
            functionCall: {
              id: "fc-123",
              name: "review_research",
              args: {},
            },
          },
        ],
      },
      longRunningToolIds: ["fc-123"],
      timestamp: Date.now(),
    };

    const result = detectPause(event);
    expect(result).not.toBeNull();
    expect(result!.functionCallId).toBe("fc-123");
    expect(result!.functionName).toBe("review_research");
    expect(result!.eventId).toBe("evt-1");
  });

  it("returns null for events without longRunningToolIds", () => {
    const event: AgentEvent = {
      id: "evt-2",
      invocationId: "inv-1",
      author: "root_agent",
      content: {
        role: "assistant",
        parts: [{ text: "Processing..." }],
      },
      timestamp: Date.now(),
    };

    expect(detectPause(event)).toBeNull();
  });

  it("returns null for empty longRunningToolIds", () => {
    const event: AgentEvent = {
      id: "evt-3",
      invocationId: "inv-1",
      author: "root_agent",
      longRunningToolIds: [],
      timestamp: Date.now(),
    };

    expect(detectPause(event)).toBeNull();
  });

  it("returns null when function call ID does not match longRunningToolIds", () => {
    const event: AgentEvent = {
      id: "evt-4",
      invocationId: "inv-1",
      author: "root_agent",
      content: {
        role: "assistant",
        parts: [
          {
            functionCall: {
              id: "fc-999",
              name: "some_tool",
              args: {},
            },
          },
        ],
      },
      longRunningToolIds: ["fc-other"],
      timestamp: Date.now(),
    };

    expect(detectPause(event)).toBeNull();
  });

  it("handles events with no content", () => {
    const event: AgentEvent = {
      id: "evt-5",
      invocationId: "inv-1",
      author: "root_agent",
      longRunningToolIds: ["fc-123"],
      timestamp: Date.now(),
    };

    expect(detectPause(event)).toBeNull();
  });

  it("detects review_ad_copies pause", () => {
    const event: AgentEvent = {
      id: "evt-6",
      invocationId: "inv-1",
      author: "root_agent",
      content: {
        role: "assistant",
        parts: [
          {
            functionCall: {
              id: "fc-456",
              name: "review_ad_copies",
              args: {},
            },
          },
        ],
      },
      longRunningToolIds: ["fc-456"],
      timestamp: Date.now(),
    };

    const result = detectPause(event);
    expect(result).not.toBeNull();
    expect(result!.functionName).toBe("review_ad_copies");
  });

  it("detects review_visual_concepts pause", () => {
    const event: AgentEvent = {
      id: "evt-7",
      invocationId: "inv-1",
      author: "root_agent",
      content: {
        role: "assistant",
        parts: [
          {
            functionCall: {
              id: "fc-789",
              name: "review_visual_concepts",
              args: {},
            },
          },
        ],
      },
      longRunningToolIds: ["fc-789"],
      timestamp: Date.now(),
    };

    const result = detectPause(event);
    expect(result).not.toBeNull();
    expect(result!.functionName).toBe("review_visual_concepts");
  });

  it("detects review_trends pause", () => {
    const event: AgentEvent = {
      id: "evt-8",
      invocationId: "inv-1",
      author: "trend_scout",
      content: {
        role: "assistant",
        parts: [
          {
            functionCall: {
              id: "fc-trends",
              name: "review_trends",
              args: {},
            },
          },
        ],
      },
      longRunningToolIds: ["fc-trends"],
      timestamp: Date.now(),
    };

    const result = detectPause(event);
    expect(result).not.toBeNull();
    expect(result!.functionCallId).toBe("fc-trends");
    expect(result!.functionName).toBe("review_trends");
    expect(result!.eventId).toBe("evt-8");
  });
});

// Test resumeRun request body construction.
//
// The async-job run model moves the `functionResponse`-message construction
// server-side: the client now POSTs a flat review payload to
// /runs/{app}/{user}/{sid}/resume (see poll-run.test.ts for the real function).

describe("Resume run request body", () => {
  it("constructs the flat resume payload with functionCallEventId", () => {
    const functionCallId = "fc-123";
    const functionName = "review_research";
    const functionCallEventId = "evt-1";
    const response = { status: "approved", feedback: "Looks good" };

    const body = {
      functionCallId,
      functionName,
      response,
      functionCallEventId,
    };

    expect(body.functionCallId).toBe("fc-123");
    expect(body.functionName).toBe("review_research");
    expect(body.response).toEqual({
      status: "approved",
      feedback: "Looks good",
    });
    expect(body.functionCallEventId).toBe("evt-1");
  });

  it("handles empty feedback in response", () => {
    const response = { status: "approved", feedback: "" };

    const body = {
      functionCallId: "fc-1",
      functionName: "review_ad_copies",
      response,
    };

    expect(body.response.status).toBe("approved");
    expect(body.response.feedback).toBe("");
  });

  it("constructs the review_trends selection payload", () => {
    const response = {
      status: "selected",
      selected_trends: ["Trend A", "Trend C"],
      instruction: "focus on pop culture",
    };

    const body = {
      functionCallId: "fc-trends",
      functionName: "review_trends",
      response,
      functionCallEventId: "evt-8",
    };

    expect(body.functionName).toBe("review_trends");
    expect(body.response.status).toBe("selected");
    expect(body.response.selected_trends).toEqual(["Trend A", "Trend C"]);
    expect(body.response.instruction).toBe("focus on pop culture");
  });
});
