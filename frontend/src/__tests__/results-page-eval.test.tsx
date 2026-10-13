// @vitest-environment jsdom
import { Suspense } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  listArtifacts: vi.fn(),
  getArtifact: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

import ResultsPage from "@/app/results/[sessionId]/page";

// A malformed or older eval report must leave the page in its "no evaluation"
// state rather than throw while rendering the summary.

const FINISHED = {
  gcs_bucket_name: "b",
  gcs_folder: "f",
  agent_output_dir: "d",
  finalize_done: true,
};

const session = (state: Record<string, unknown>): Session =>
  ({ id: "s1", appName: "creative_agent", userId: "me", state, events: [] }) as unknown as Session;

function renderPage() {
  const params = Object.assign(Promise.resolve({ sessionId: "s1" }), {
    status: "fulfilled",
    value: { sessionId: "s1" },
  });
  return render(
    <Suspense fallback={null}>
      <ResultsPage params={params} />
    </Suspense>
  );
}

const fetchMock = vi.fn();

beforeEach(() => {
  api.getSession.mockReset().mockResolvedValue(session(FINISHED));
  api.listArtifacts.mockReset().mockResolvedValue([]);
  api.getArtifact.mockReset();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

describe("results page: eval report parsing", () => {
  it("shows the summary for a well-formed report", async () => {
    fetchMock.mockResolvedValue(
      json({
        ad_copy_evaluations: [],
        visual_concept_evaluations: [],
        summary: {
          total_ad_copies: 4,
          ad_copies_passed: 3,
          avg_ad_copy_score: 0.8,
          total_visual_concepts: 0,
          visual_concepts_passed: 0,
          avg_visual_score: 0,
          overall_pass_rate: 0.75,
          weakest_dimensions: [],
        },
      })
    );
    renderPage();
    expect(await screen.findByText(/3 of 4 ad copies pass\./)).toBeInTheDocument();
  });

  it.each([
    ["an array", [1, 2]],
    ["a report without a summary", { ad_copy_evaluations: "oops" }],
    ["a string", "not a report"],
  ])("falls back to the no-evaluation state for %s", async (_label, body) => {
    fetchMock.mockResolvedValue(json(body));
    renderPage();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText("Loading the evaluation report…")).toBeNull());
    expect(screen.queryByText(/didn.t load/)).toBeNull();
    expect(screen.queryByText(/ pass\./)).toBeNull();
    // Still rendered (a throw while rendering would unmount the page).
    expect(screen.getByRole("heading", { level: 1, name: "Results" })).toBeInTheDocument();
  });
});
