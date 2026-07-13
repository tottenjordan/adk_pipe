import { describe, it, expect } from "vitest";
import { PROXY_STREAM_TIMEOUTS } from "@/app/api/adk/[...path]/route";

// The /api/adk proxy forwards ADK `/run_sse` responses, which stream for many minutes.
// Node's global fetch (undici) defaults `bodyTimeout` and `headersTimeout` to 300_000ms
// (5 min) — an *idle* timeout that aborts the response body when a quota-paced run has a
// gap longer than 5 min between SSE events, surfacing as `UND_ERR_BODY_TIMEOUT` and a
// truncated / blank run. The proxy passes a dispatcher built from this config to disable
// both. 0 means "no timeout".
describe("PROXY_STREAM_TIMEOUTS", () => {
  it("disables undici's idle body timeout so long SSE runs are not terminated", () => {
    expect(PROXY_STREAM_TIMEOUTS.bodyTimeout).toBe(0);
  });

  it("disables the headers timeout for backends slow to emit the first byte", () => {
    expect(PROXY_STREAM_TIMEOUTS.headersTimeout).toBe(0);
  });
});
