// @vitest-environment jsdom
import { Suspense } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseGtrendsStaleness, trendStalenessText } from "@/lib/trend-staleness";
import { TrendStalenessNotice } from "@/components/trend-staleness-notice";
import { ReviewPanel } from "@/app/run/[sessionId]/ReviewPanel";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=trend_scout&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import RunPage from "@/app/run/[sessionId]/page";

// trend_scout's get_daily_gtrends writes state `gtrends_staleness` =
// {refresh_date, age_days, stale, threshold_days} (agents/trend_scout/staleness.py).
const STALE = { refresh_date: "2026-10-13", age_days: 6, stale: true, threshold_days: 2 };
const FRESH = { refresh_date: "2026-10-18", age_days: 1, stale: false, threshold_days: 2 };
const TEXT =
  "Google's trend data was last updated Oct 13 (6 days ago); trends may be out of date.";

describe("parseGtrendsStaleness", () => {
  it("reads the object or its JSON string", () => {
    const parsed = { refreshDate: "2026-10-13", ageDays: 6, stale: true };
    expect(parseGtrendsStaleness(STALE)).toEqual(parsed);
    expect(parseGtrendsStaleness(JSON.stringify(STALE))).toEqual(parsed);
  });

  it("rejects missing or malformed values", () => {
    for (const raw of [
      undefined,
      null,
      "",
      "nope",
      42,
      {},
      { ...STALE, refresh_date: "2026-13-40" },
      { ...STALE, age_days: "6" },
      { ...STALE, age_days: -1 },
      { ...STALE, age_days: 1.5 },
      { ...STALE, stale: "yes" },
    ]) {
      expect(parseGtrendsStaleness(raw)).toBeNull();
    }
  });
});

describe("trendStalenessText", () => {
  it("states the last update and age only when stale", () => {
    expect(trendStalenessText({ gtrends_staleness: STALE })).toBe(TEXT);
    expect(trendStalenessText({ gtrends_staleness: FRESH })).toBe("");
    expect(trendStalenessText({})).toBe("");
  });

  it("uses the singular for one day", () => {
    expect(
      trendStalenessText({
        gtrends_staleness: { ...STALE, refresh_date: "2026-10-18", age_days: 1 },
      })
    ).toContain("(1 day ago)");
  });
});

describe("TrendStalenessNotice", () => {
  it("renders a status notice when stale", () => {
    render(<TrendStalenessNotice state={{ gtrends_staleness: STALE }} />);
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(TEXT);
    expect(notice.className).toContain("mark-pending");
  });

  it("renders nothing when fresh or missing", () => {
    const fresh = render(<TrendStalenessNotice state={{ gtrends_staleness: FRESH }} />);
    expect(fresh.container).toBeEmptyDOMElement();
    const none = render(<TrendStalenessNotice state={{}} />);
    expect(none.container).toBeEmptyDOMElement();
  });
});

describe("trend pick panel", () => {
  const renderPick = (state: Record<string, unknown>) =>
    render(
      <ReviewPanel
        functionName="review_trends"
        sessionState={{ raw_gtrends: ["a", "b"], gtrends_refresh_date: "2026-10-13", ...state }}
        onResume={vi.fn()}
      />
    );

  it("shows the stale notice", () => {
    renderPick({ gtrends_staleness: STALE });
    expect(screen.getByRole("status")).toHaveTextContent(TEXT);
  });

  it("shows no notice for fresh data", () => {
    renderPick({ gtrends_staleness: FRESH });
    expect(screen.queryByText(/may be out of date/)).not.toBeInTheDocument();
  });
});

describe("run page", () => {
  const EVENT = {
    id: "e1",
    invocationId: "inv",
    author: "gather_trends_agent",
    timestamp: 1,
    content: { role: "model", parts: [{ text: "Retrieved 25 trends." }] },
  };

  function json(body: unknown) {
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  const PICK_CALL = {
    id: "e2",
    invocationId: "inv",
    author: "trend_scout",
    timestamp: 2,
    longRunningToolIds: ["fc-pick"],
    content: {
      role: "model",
      parts: [{ functionCall: { id: "fc-pick", name: "review_trends", args: {} } }],
    },
  };

  function renderPage(
    state: Record<string, unknown>,
    { status = "running", events = [EVENT] }: { status?: string; events?: unknown[] } = {}
  ) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/runs/"))
          return json({ status, events, nextCursor: events.length, state, error: null });
        return json({ state });
      })
    );
    const params = Object.assign(Promise.resolve({ sessionId: "s1" }), {
      status: "fulfilled",
      value: { sessionId: "s1" },
    });
    return render(
      <Suspense fallback={null}>
        <RunPage params={params} searchParams={Promise.resolve({})} />
      </Suspense>
    );
  }

  beforeEach(() => sessionStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("shows the stale notice from session state", async () => {
    renderPage({ gtrends_staleness: STALE });
    await waitFor(() => expect(screen.getByText(TEXT)).toBeInTheDocument());
    expect(screen.getByText(TEXT).closest("[role=status]")).not.toBeNull();
  });

  it("shows the notice once while the trend pick panel is open", async () => {
    renderPage(
      { gtrends_staleness: STALE, raw_gtrends: ["a", "b"] },
      { status: "done", events: [EVENT, PICK_CALL] }
    );
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Pick your trends" })).toBeInTheDocument()
    );
    expect(screen.getAllByText(TEXT)).toHaveLength(1);
  });

  it("shows nothing for fresh data", async () => {
    renderPage({ gtrends_staleness: FRESH, brand: "Acme" });
    await waitFor(() => expect(screen.getAllByText(/Acme/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/may be out of date/)).not.toBeInTheDocument();
  });
});
