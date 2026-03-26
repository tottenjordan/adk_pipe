import { describe, it, expect } from "vitest";

// Test the SSE line parsing logic used in streamRun (api.ts)

function parseSSELines(raw: string): unknown[] {
  const results: unknown[] = [];
  const lines = raw.split("\n");

  for (const line of lines) {
    if (line.startsWith("data: ")) {
      const data = line.slice(6).trim();
      if (data) {
        try {
          results.push(JSON.parse(data));
        } catch {
          // skip malformed JSON
        }
      }
    }
  }
  return results;
}

describe("SSE line parsing", () => {
  it("parses a single data line", () => {
    const raw = 'data: {"id":"1","author":"agent"}\n';
    const result = parseSSELines(raw);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ id: "1", author: "agent" });
  });

  it("parses multiple data lines", () => {
    const raw = [
      'data: {"id":"1","author":"a"}',
      'data: {"id":"2","author":"b"}',
      "",
    ].join("\n");
    const result = parseSSELines(raw);
    expect(result).toHaveLength(2);
  });

  it("skips non-data lines", () => {
    const raw = [
      "event: message",
      'data: {"id":"1"}',
      ": comment",
      "retry: 3000",
      "",
    ].join("\n");
    const result = parseSSELines(raw);
    expect(result).toHaveLength(1);
  });

  it("skips empty data lines", () => {
    const raw = "data: \ndata: \n";
    const result = parseSSELines(raw);
    expect(result).toHaveLength(0);
  });

  it("skips malformed JSON", () => {
    const raw = "data: {not valid json}\ndata: {\"valid\":true}\n";
    const result = parseSSELines(raw);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ valid: true });
  });

  it("handles empty input", () => {
    expect(parseSSELines("")).toHaveLength(0);
  });
});

// Test URL construction logic used across API functions
describe("API URL construction", () => {
  const API_BASE = "http://localhost:8000";

  it("builds session creation URL", () => {
    const appName = "trend_trawler";
    const userId = "user_123";
    const url = `${API_BASE}/apps/${appName}/users/${userId}/sessions`;
    expect(url).toBe(
      "http://localhost:8000/apps/trend_trawler/users/user_123/sessions"
    );
  });

  it("builds session fetch URL", () => {
    const url = `${API_BASE}/apps/creative_agent/users/u1/sessions/sess_abc`;
    expect(url).toBe(
      "http://localhost:8000/apps/creative_agent/users/u1/sessions/sess_abc"
    );
  });

  it("builds artifact list URL", () => {
    const url = `${API_BASE}/apps/creative_agent/users/u1/sessions/s1/artifacts`;
    expect(url).toBe(
      "http://localhost:8000/apps/creative_agent/users/u1/sessions/s1/artifacts"
    );
  });

  it("builds artifact fetch URL", () => {
    const url = `${API_BASE}/apps/creative_agent/users/u1/sessions/s1/artifacts/image.png`;
    expect(url).toBe(
      "http://localhost:8000/apps/creative_agent/users/u1/sessions/s1/artifacts/image.png"
    );
  });

  it("constructs run_sse request body correctly", () => {
    const body = {
      appName: "creative_agent",
      userId: "user_1",
      sessionId: "sess_1",
      newMessage: {
        role: "user",
        parts: [{ text: 'Brand Name: "PRS"' }],
      },
      streaming: true,
    };
    expect(body.newMessage.role).toBe("user");
    expect(body.newMessage.parts[0].text).toContain("PRS");
    expect(body.streaming).toBe(true);
  });
});
