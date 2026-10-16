// @vitest-environment jsdom
import { Suspense } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  listArtifacts: vi.fn(),
  listShares: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

import ResultsPage from "@/app/results/[sessionId]/page";

// The results page is ordered for daily review: the contact sheet first, then the
// brief and report, shared links, and only then the (collapsed) experiment set-up.

const CONCEPTS = ["Alpha", "Bravo"];
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
  creative_brief: SAMPLE_CREATIVE_BRIEF,
  combined_final_cited_report: "## Market pulse\n\nReport body.",
};

const session = (): Session =>
  ({ id: "s1", appName: "creative_agent", userId: "me", state: STATE, events: [] }) as unknown as Session;

const SHARE = {
  token: "t1",
  url: "/s/t1",
  title: "Slate share",
  scope: "slate",
  include_eval: false,
  concept_names: CONCEPTS,
  app_name: "creative_agent",
  session_id: "s1",
  created_at: "2026-10-01T10:00:00Z",
};

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

beforeEach(() => {
  api.getSession.mockReset().mockResolvedValue(session());
  api.listArtifacts.mockReset().mockResolvedValue(["research.pdf"]);
  api.listShares.mockReset().mockResolvedValue([SHARE]);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 404 }))
  );
});
afterEach(() => vi.unstubAllGlobals());

/** True when `a` comes before `b` in document order. */
const before = (a: Element, b: Element) =>
  Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

describe("results page: section order", () => {
  it("puts the contact sheet first and the experiment set-up after brief, report and shares", async () => {
    renderPage();
    const sheet = await screen.findByRole("region", { name: /^Creatives/ });
    const shares = await screen.findByRole("region", { name: "Shared links" });
    const landmarks = [
      sheet,
      screen.getByRole("button", { name: "Creative brief" }),
      screen.getByRole("button", { name: "Research report" }),
      shares,
      screen.getByRole("region", { name: "Deploy creatives as a live experiment" }),
      screen.getByRole("button", { name: /^Artifacts/ }),
      screen.getByRole("button", { name: "Session state" }),
    ];
    for (let i = 1; i < landmarks.length; i++) {
      expect(before(landmarks[i - 1], landmarks[i]), `landmark ${i - 1} before ${i}`).toBe(true);
    }
  });
});

describe("results page: deploy panel disclosure", () => {
  it("is collapsed by default (panel not mounted) and opens on click", async () => {
    renderPage();
    const trigger = await screen.findByRole("button", { name: "Deploy creatives as a live experiment" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    // The anchor stays put whether or not the panel is open.
    expect(document.getElementById("deploy-heading")).not.toBeNull();
    expect(screen.queryByText(/Creatives to test/)).toBeNull();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText(/Creatives to test/)).toBeInTheDocument();
  });
});

describe("results page: loading and errors", () => {
  it("shows a contact-sheet skeleton while loading, then the creatives", async () => {
    let resolve!: (s: Session) => void;
    api.getSession.mockReturnValue(new Promise<Session>((r) => (resolve = r)));
    const { container } = renderPage();

    expect(screen.getByRole("status")).toHaveTextContent("Loading results…");
    expect(container.querySelectorAll('[data-slot="skeleton-card"]')).toHaveLength(4);

    resolve(session());
    expect(await screen.findByRole("region", { name: /^Creatives/ })).toBeInTheDocument();
    expect(container.querySelectorAll('[data-slot="skeleton-card"]')).toHaveLength(0);
  });

  it("shows an error with a retry that loads the results again", async () => {
    api.getSession.mockReset().mockRejectedValueOnce(new Error("Session not found")).mockResolvedValue(session());
    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("These results didn't load");
    expect(alert).toHaveTextContent("Session not found");
    expect(screen.getByRole("link", { name: "Open run history" })).toHaveAttribute("href", "/runs");

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: /^Creatives/ })).toBeInTheDocument();
    expect(api.getSession).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByText("These results didn't load")).toBeNull());
  });
});
