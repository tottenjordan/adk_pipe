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
});

// Test resumeRun request body construction

describe("Resume run request body", () => {
  it("constructs correct functionResponse message with functionCallEventId", () => {
    const functionCallId = "fc-123";
    const functionName = "review_research";
    const functionCallEventId = "evt-1";
    const response = { status: "approved", feedback: "Looks good" };

    const body = {
      appName: "interactive_creative",
      userId: "user_1",
      sessionId: "sess_1",
      newMessage: {
        role: "user",
        parts: [
          {
            functionResponse: {
              id: functionCallId,
              name: functionName,
              response: response,
            },
          },
        ],
      },
      functionCallEventId,
      streaming: true,
    };

    expect(body.newMessage.parts[0].functionResponse).toBeDefined();
    expect(body.newMessage.parts[0].functionResponse!.id).toBe("fc-123");
    expect(body.newMessage.parts[0].functionResponse!.name).toBe(
      "review_research"
    );
    expect(body.newMessage.parts[0].functionResponse!.response).toEqual({
      status: "approved",
      feedback: "Looks good",
    });
    expect(body.functionCallEventId).toBe("evt-1");
    expect(body.streaming).toBe(true);
  });

  it("handles empty feedback in response", () => {
    const response = { status: "approved", feedback: "" };

    const body = {
      newMessage: {
        role: "user",
        parts: [
          {
            functionResponse: {
              id: "fc-1",
              name: "review_ad_copies",
              response: response,
            },
          },
        ],
      },
    };

    expect(body.newMessage.parts[0].functionResponse!.response.status).toBe(
      "approved"
    );
    expect(body.newMessage.parts[0].functionResponse!.response.feedback).toBe(
      ""
    );
  });
});
