// @vitest-environment jsdom
import { Suspense } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  listArtifacts: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

import ResultsPage from "@/app/results/[sessionId]/page";

// The share dialogs get the failed blocking checks of the creatives they would share:
// the slate gets every rendered proof's, "Share this creative" just that proof's.

const CONCEPTS = ["Alpha", "Bravo", "Charlie"];
const STATE = {
  gcs_bucket_name: "b",
  gcs_folder: "f",
  agent_output_dir: "d",
  finalize_done: true,
  _images_generated: true,
  final_visual_concepts: {
    visual_concepts: CONCEPTS.map((name, i) => ({
      concept_name: name,
      headline: `Headline ${name}`,
      ad_copy_id: i + 1,
      concept_summary: `Summary ${name}`,
    })),
  },
  ad_copy_critique: { ad_copies: [] },
  generated_images: Object.fromEntries(CONCEPTS.map((c) => [c, { gcs_uri: `gs://b/f/d/${c}.png` }])),
};

type Gate = { gate: string; passed: boolean; advisory?: boolean };

const score = (gates?: Gate[]) => ({
  overall_score: 0.8,
  passed: true,
  verdicts: [],
  strengths: [],
  improvements: [],
  ...(gates ? { gates } : {}),
});

const report = (gated: boolean) => {
  const gates: Record<string, Gate[]> = {
    Alpha: [{ gate: "no_visual_defects", passed: false }],
    Bravo: [
      { gate: "product_visible", passed: false },
      { gate: "brand_cue_present", passed: false, advisory: true },
    ],
    Charlie: [{ gate: "product_visible", passed: true }],
  };
  return {
    ad_copy_evaluations: [],
    visual_concept_evaluations: CONCEPTS.map((name, i) => ({
      ad_copy_id: i + 1,
      concept_name: name,
      score: score(gated ? gates[name] : undefined),
    })),
    summary: {
      total_ad_copies: 0,
      ad_copies_passed: 0,
      avg_ad_copy_score: 0,
      total_visual_concepts: 3,
      visual_concepts_passed: 1,
      avg_visual_score: 0.8,
      overall_pass_rate: 0.33,
      weakest_dimensions: [],
    },
  };
};

const session = (): Session =>
  ({ id: "s1", appName: "creative_agent", userId: "me", state: STATE, events: [] }) as unknown as Session;

function renderPage() {
  const params = Object.assign(Promise.resolve({ sessionId: "s1" }), {
    status: "fulfilled",
    value: { sessionId: "s1" },
  });
  return render(
    <Suspense fallback={null}>
      <ResultsPage params={params} searchParams={Promise.resolve({})} />
    </Suspense>
  );
}

function stubReport(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("creative_eval_report.json")
        ? new Response(JSON.stringify(body), { status: 200 })
        : new Response("{}", { status: 404 })
    )
  );
}

beforeEach(() => {
  api.getSession.mockReset().mockResolvedValue(session());
  api.listArtifacts.mockReset().mockResolvedValue([]);
});
afterEach(() => vi.unstubAllGlobals());

describe("results page: failed checks in the share dialogs", () => {
  it("gives the slate dialog every failing creative's blocking checks", async () => {
    stubReport(report(true));
    renderPage();
    await screen.findAllByText("Check failed");
    fireEvent.click(screen.getByRole("button", { name: "Share slate" }));
    expect(await screen.findByRole("note")).toHaveTextContent(
      "2 of these creatives failed a quality check: Alpha (No visual defects); Bravo (Product visible)."
    );
  });

  it("gives a single creative's dialog only that creative's checks", async () => {
    stubReport(report(true));
    renderPage();
    await screen.findAllByText("Check failed");
    fireEvent.click(screen.getByRole("button", { name: /Open creative 2: Headline Bravo/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Share this creative" }));
    const dialog = await screen.findByRole("dialog", { name: "Share this creative" });
    expect(within(dialog).getByRole("note")).toHaveTextContent(
      "This creative failed a quality check (Product visible). Anyone with the link will see it."
    );
  });

  it("shows no note for a gate-less report", async () => {
    stubReport(report(false));
    renderPage();
    await screen.findByText(/1 of 3 visuals pass\./);
    fireEvent.click(screen.getByRole("button", { name: "Share slate" }));
    await screen.findByRole("button", { name: "Create link" });
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });
});
