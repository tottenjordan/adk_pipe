import { describe, it, expect } from "vitest";
import type { AgentEvent } from "@/lib/types";

import { PendingLongRunningCalls, isPauseAnswered } from "@/lib/pause-detection";

// Pause detection used by the run page (lib/pause-detection). A single
// unanswered long-running call is a pause.
function detectPause(event: AgentEvent) {
  const pending = new PendingLongRunningCalls();
  pending.observe(event);
  return pending.pause();
}

function call(id: string, name: string, eventId: string): AgentEvent {
  return {
    id: eventId,
    invocationId: "inv-1",
    author: "root_agent",
    content: { role: "model", parts: [{ functionCall: { id, name, args: {} } }] },
    longRunningToolIds: [id],
    timestamp: Date.now(),
  };
}

function answer(
  id: string,
  name: string,
  eventId: string,
  author = "root_agent"
): AgentEvent {
  return {
    id: eventId,
    invocationId: "inv-1",
    author,
    content: {
      role: "user",
      parts: [{ functionResponse: { id, name, response: { result: "ok" } } }],
    },
    timestamp: Date.now(),
  };
}

function text(eventId: string, author: string, body: string): AgentEvent {
  return {
    id: eventId,
    invocationId: "inv-1",
    author,
    content: { role: "model", parts: [{ text: body }] },
    timestamp: Date.now(),
  };
}

function segmentPause(events: AgentEvent[]) {
  const pending = new PendingLongRunningCalls();
  for (const e of events) pending.observe(e);
  return pending.pause();
}

// P2 graph-Workflow migration: pipeline tools are ADK NodeTools, which are
// long-running too, so their call events carry longRunningToolIds. Only the
// call left UNANSWERED at the end of the poll segment is the pause.
describe("Pause detection with long-running pipeline NodeTools", () => {
  it("does not pause on an answered pipeline NodeTool call", () => {
    const events = [
      call("fc-research", "combined_research_pipeline", "e1"),
      // inner pipeline event on the tool's branch, authored by an inner agent
      text("e2", "combined_report_composer", "# Report"),
      answer("fc-research", "combined_research_pipeline", "e3"),
      text("e4", "root_agent", "DONE"),
    ];
    expect(segmentPause(events)).toBeNull();
  });

  it("pauses on the checkpoint, not the earlier pipeline call", () => {
    const events = [
      call("fc-research", "combined_research_pipeline", "e1"),
      answer("fc-research", "combined_research_pipeline", "e2"),
      call("fc-cp1", "review_research", "e3"),
    ];
    expect(segmentPause(events)).toEqual({
      functionCallId: "fc-cp1",
      functionName: "review_research",
      eventId: "e3",
    });
  });

  it("does not re-pause when replaying a finished run from since=0", () => {
    const events = [
      call("fc-cp1", "review_research", "e1"),
      answer("fc-cp1", "review_research", "e2", "user"),
      call("fc-ads", "ad_creative_pipeline", "e3"),
      answer("fc-ads", "ad_creative_pipeline", "e4"),
    ];
    expect(segmentPause(events)).toBeNull();
  });

  it("ignores partial (streaming) events", () => {
    const partial = { ...call("fc-1", "review_research", "e1"), partial: true };
    expect(detectPause(partial)).toBeNull();
  });
});

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

// A paused tab must notice when the checkpoint is answered elsewhere (another
// tab or a reload resumed the run), otherwise it shows "Waiting for review"
// forever. The paused page's watcher uses isPauseAnswered on each poll.
describe("isPauseAnswered (stale paused tab)", () => {
  it("is false while the checkpoint call is unanswered", () => {
    const events = [call("lr-1", "review_trends", "e1")];
    expect(isPauseAnswered(events, "lr-1")).toBe(false);
  });

  it("is true once a functionResponse for the paused call appears", () => {
    const events = [
      call("lr-1", "review_trends", "e1"),
      answer("lr-1", "review_trends", "e2", "user"),
    ];
    expect(isPauseAnswered(events, "lr-1")).toBe(true);
  });

  it("ignores responses to other calls", () => {
    const events = [answer("other", "pick_trends_agent", "e2")];
    expect(isPauseAnswered(events, "lr-1")).toBe(false);
  });

  it("ignores partial (streaming) events", () => {
    const partial = { ...answer("lr-1", "review_trends", "e2", "user"), partial: true };
    expect(isPauseAnswered([partial], "lr-1")).toBe(false);
  });
});
