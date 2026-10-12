// @vitest-environment jsdom
import { Suspense } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import RunPage from "@/app/run/[sessionId]/page";

// The run page follows a detached run via GET /runs polls. An error event a
// fail-soft step contained (e.g. trend_situation_synthesizer's model 400) must
// not mark the run failed; only the poll payload's status decides that.

const agentEvent = (id: string, over: Partial<AgentEvent> = {}): AgentEvent =>
  ({
    id,
    invocationId: "inv",
    author: "root_agent",
    timestamp: 1,
    content: { role: "model", parts: [{ text: `text ${id}` }] },
    ...over,
  }) as AgentEvent;

const stepError = agentEvent("err", {
  author: "trend_situation_synthesizer",
  content: undefined,
  errorCode: "INVALID_ARGUMENT",
  errorMessage: "400 INVALID_ARGUMENT: bad schema",
});

function json(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function mockBackend(polls: unknown[]) {
  let i = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.includes("/runs/")) return json(polls[Math.min(i++, polls.length - 1)]);
      return json({ state: {} }); // getSession
    })
  );
}

function renderPage() {
  // Pre-fulfilled (React's own thenable fields), so `use(params)` reads it
  // synchronously instead of suspending.
  const params = Object.assign(Promise.resolve({ sessionId: "s1" }), {
    status: "fulfilled",
    value: { sessionId: "s1" },
  });
  return render(
    <Suspense fallback={null}>
      <RunPage params={params} />
    </Suspense>
  );
}

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("run page: contained step errors", () => {
  it("shows a non-blocking notice and completes when the poll says done", async () => {
    const events = [agentEvent("e1"), stepError, agentEvent("e2")];
    // 1st fetch = the getRunStatus seed, then pollRun: running, then done.
    mockBackend([
      { status: "running", events, nextCursor: 3, state: { eval_report_gcs_uri: "gs://b/report.json" }, error: null },
      { status: "running", events, nextCursor: 3, state: {}, error: null },
      { status: "done", events: [], nextCursor: 3, state: {}, error: null },
    ]);
    renderPage();
    expect(
      await screen.findByText(
        /A step reported an error and was skipped: 400 INVALID_ARGUMENT/
      )
    ).toBeInTheDocument();
    await waitFor(
      () => expect(screen.getByRole("button", { name: "View results" })).toBeInTheDocument(),
      { timeout: 5000 }
    );
    expect(screen.queryByRole("alert")).toBeNull();
  }, 10_000);

  it("still fails the run when the poll status is error", async () => {
    mockBackend([
      { status: "running", events: [agentEvent("e1")], nextCursor: 1, state: {}, error: null },
      { status: "running", events: [agentEvent("e1")], nextCursor: 1, state: {}, error: null },
      { status: "error", events: [], nextCursor: 1, state: {}, error: "root model 429 boom" },
    ]);
    renderPage();
    const alert = await screen.findByRole("alert", {}, { timeout: 5000 });
    expect(alert).toHaveTextContent("root model 429 boom");
  }, 10_000);
});
