// @vitest-environment jsdom
import { Suspense } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/lib/types";

let search = "app=creative_agent&userId=me";
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(search),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import RunPage from "@/app/run/[sessionId]/page";
import { RUN_STALL_TIMEOUT_MS, stallTimeoutFor } from "@/app/run/[sessionId]/run-config";

// A run page follows a detached run that keeps going on the server whatever
// happens to the browser's connection. A poll outage must read as a lost
// connection (reload), not a failed run (duplicate), and a long quiet render
// must not stay flagged as stalled once events flow again.

const agentEvent = (id: string): AgentEvent =>
  ({
    id,
    invocationId: "inv",
    author: "root_agent",
    timestamp: 1,
    content: { role: "model", parts: [{ text: `text ${id}` }] },
  }) as AgentEvent;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * A backend whose run log is `serverEvents` (polls answer `since` correctly).
 * After `okPolls` successful /runs calls (the seed counts), it answers 503.
 */
function mockBackend(serverEvents: AgentEvent[], okPolls = Infinity) {
  let runsCalls = 0;
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes("/runs/")) {
      runsCalls += 1;
      if (runsCalls > okPolls) return new Response("unavailable", { status: 503 });
      const since = Number(new URL(url, "http://x").searchParams.get("since") ?? 0);
      return json({
        status: "running",
        events: serverEvents.slice(since),
        nextCursor: serverEvents.length,
        state: {},
        error: null,
      });
    }
    return json({ state: {} }); // getSession
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPage() {
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

/**
 * Advance fake time in 1 s steps, each in its own act(): React flushes an async
 * act's updates (and their effects) only when it ends, so one long act would
 * commit the first events minutes late and skew the stall clock.
 */
async function advance(ms: number) {
  for (let left = ms; left > 0; left -= 1_000) {
    await act(() => vi.advanceTimersByTimeAsync(Math.min(1_000, left)));
  }
}
const MIN = 60_000;

beforeEach(() => {
  sessionStorage.clear();
  search = "app=creative_agent&userId=me";
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("run page: lost connection", () => {
  it("shows a reconnect banner, not a failure, when polls keep failing", async () => {
    // Seed + first poll succeed, then the backend is unreachable for good.
    mockBackend([agentEvent("e1")], 2);
    renderPage();
    await advance(60_000); // all poll retries (1+2+4+8+15 s) exhausted

    expect(screen.getByText(/Lost connection to the run/)).toBeInTheDocument();
    expect(screen.getByText("Connection lost")).toBeInTheDocument();
    expect(screen.queryByText(/Failed/)).toBeNull();
    expect(screen.queryByText(/Duplicate/)).toBeNull();
    expect(screen.queryByRole("alert", { name: /failed/i })).toBeNull();
  });

  it("reloads the page from the banner to reconnect", async () => {
    mockBackend([agentEvent("e1")], 2);
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    renderPage();
    await advance(60_000);

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("rides out a short outage without leaving the running state", async () => {
    const events = [agentEvent("e1")];
    // Seed + one poll ok, then 503s; the backend recovers before retries run out.
    const fetchMock = mockBackend(events, 2);
    renderPage();
    await advance(5_000); // a few failed retries
    fetchMock.mockImplementation(async (url: string) =>
      url.includes("/runs/")
        ? json({ status: "running", events: [], nextCursor: 1, state: {}, error: null })
        : json({ state: {} })
    );
    await advance(20_000);

    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.queryByText(/Lost connection/)).toBeNull();
  });
});

describe("stallTimeoutFor", () => {
  it.each([
    ["creative_agent", 8 * MIN],
    ["interactive_creative", 8 * MIN],
    ["trend_scout", RUN_STALL_TIMEOUT_MS],
    ["unknown_app", RUN_STALL_TIMEOUT_MS],
  ])("%s → %i ms", (app, ms) => {
    expect(stallTimeoutFor(app)).toBe(ms);
  });

  it("keeps the 3-minute default for other apps", () => {
    expect(RUN_STALL_TIMEOUT_MS).toBe(3 * MIN);
  });
});

describe("run page: stall watchdog", () => {
  it("clears 'No recent activity' as soon as a new event arrives", async () => {
    search = "app=trend_scout&userId=me";
    const events = [agentEvent("e1")];
    mockBackend(events);
    renderPage();
    await advance(3 * MIN + 20_000);
    expect(screen.getAllByText("No recent activity").length).toBeGreaterThan(0);

    events.push(agentEvent("e2"));
    await advance(3_000);

    expect(screen.queryAllByText("No recent activity")).toHaveLength(0);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("gives a creative run 8 minutes of silence before flagging it", async () => {
    mockBackend([agentEvent("e1")]);
    renderPage();
    await advance(4 * MIN);
    expect(screen.queryAllByText("No recent activity")).toHaveLength(0);

    await advance(5 * MIN);
    expect(screen.getAllByText("No recent activity").length).toBeGreaterThan(0);
  });
});
