// @vitest-environment jsdom
import { Suspense } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  listArtifacts: vi.fn(),
  listPersonRefs: vi.fn(),
  listVariants: vi.fn(),
  createVariant: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

import ResultsPage from "@/app/results/[sessionId]/page";

// The proof dialog remounts the Personalise panel per creative; the page loads the
// caller's people and the session's variants once and shares them.

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

const session = (): Session =>
  ({ id: "s1", appName: "creative_agent", userId: "me", state: STATE, events: [] }) as unknown as Session;

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

beforeEach(() => {
  api.getSession.mockReset().mockResolvedValue(session());
  api.listArtifacts.mockReset().mockResolvedValue([]);
  api.listPersonRefs.mockReset().mockResolvedValue({
    personRefs: [
      {
        consent_id: "consent-1234",
        photo_uri: "gs://b/person-refs/a-1/me.jpg",
        label: "Me",
        subject: "self",
        adult_attested: true,
        allow_public_share: false,
        consent_text_version: "2026-10-09",
        created_at: "2026-10-09T00:00:00Z",
      },
    ],
    prefix: null,
  });
  api.listVariants.mockReset().mockResolvedValue({ variants: {}, castable: null, personSafeStyles: [] });
  api.createVariant.mockReset();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
});
afterEach(() => vi.unstubAllGlobals());

describe("results page: personalisation data", () => {
  it("loads nothing before a proof opens, then people and variants once across three proofs", async () => {
    renderPage();
    const open = await screen.findByRole("button", { name: /Open creative 1: Headline Alpha/ });
    expect(api.listPersonRefs).not.toHaveBeenCalled();
    expect(api.listVariants).not.toHaveBeenCalled();

    fireEvent.click(open);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Person" })).toHaveValue("consent-1234"));
    for (const name of ["Bravo", "Charlie"]) {
      fireEvent.click(screen.getByRole("button", { name: "Next creative" }));
      await screen.findByRole("heading", { name: `Headline ${name}` });
      await waitFor(() => expect(screen.getByRole("combobox", { name: "Person" })).toHaveValue("consent-1234"));
    }
    expect(api.listPersonRefs).toHaveBeenCalledTimes(1);
    expect(api.listVariants).toHaveBeenCalledTimes(1);
  });
});
