// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listPersonRefs = vi.fn();
const listVariants = vi.fn();
const createVariant = vi.fn();
vi.mock("@/lib/api", () => ({
  listPersonRefs: () => listPersonRefs(),
  listVariants: (...a: unknown[]) => listVariants(...a),
  createVariant: (...a: unknown[]) => createVariant(...a),
}));

import { PersonalisePanel } from "@/components/personalise-panel";
import {
  canPersonalise,
  latestVariant,
  MAX_POLL_MISSES,
  parseVariants,
  VARIANT_NOTE,
  VARIANT_POLL_MS,
  VariantError,
  variantErrorMessage,
  variantImageCheck,
  variantImageUrl,
} from "@/lib/variants";

const CONCEPT = "The Authentic Encore";
const REFS = [
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
  {
    consent_id: "consent-5678",
    photo_uri: "gs://b/person-refs/a-1/sam.png",
    label: "Sam",
    subject: "third_party_with_consent",
    adult_attested: true,
    allow_public_share: true,
    consent_text_version: "2026-10-09",
    created_at: "2026-10-09T00:00:00Z",
  },
];
const DONE = {
  status: "done" as const,
  consent_id: "consent-1234",
  created_at: "2026-10-10T10:00:00Z",
  gcs_uri: "gs://b/run/creative_output/variants/a-1/The_Authentic_Encore/abc123abc123.png",
  qa: { passed: false, failures: ["person likeness lost"] },
  attempts: 2,
  reason: null,
};

function renderPanel() {
  return render(
    <PersonalisePanel
      appName="creative_agent"
      sessionId="s1"
      conceptName={CONCEPT}
      baseImageUrl="/api/gcs?bucket=b&path=run%2Fcreative_output%2FThe_Authentic_Encore.png"
      baseAlt="A guitarist mid-solo"
    />,
  );
}

beforeEach(() => {
  listPersonRefs.mockReset();
  listVariants.mockReset();
  createVariant.mockReset();
  listPersonRefs.mockResolvedValue({ personRefs: REFS, prefix: null });
  listVariants.mockResolvedValue({});
});

afterEach(() => {
  vi.useRealTimers();
});

describe("PersonalisePanel", () => {
  it("renders a preview, polls while it is pending and shows both images", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    createVariant.mockResolvedValue({
      key: "abc123abc123",
      concept_name: CONCEPT,
      cached: false,
      status: "queued",
      consent_id: "consent-1234",
      created_at: DONE.created_at,
    });
    renderPanel();
    const select = await screen.findByRole("combobox", { name: "Person" });
    await waitFor(() => expect(select).toHaveValue("consent-1234"));
    expect(screen.getByText(VARIANT_NOTE)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Render preview" }));
    await waitFor(() =>
      expect(createVariant).toHaveBeenCalledWith("creative_agent", "s1", CONCEPT, "consent-1234"),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(/Queued/);
    expect(screen.getByRole("button", { name: "Rendering…" })).toBeDisabled();

    listVariants.mockResolvedValue({ [CONCEPT]: { abc123abc123: { ...DONE, status: "rendering" } } });
    await act(() => vi.advanceTimersByTimeAsync(VARIANT_POLL_MS));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Rendering the preview/));

    listVariants.mockResolvedValue({ [CONCEPT]: { abc123abc123: DONE } });
    await act(() => vi.advanceTimersByTimeAsync(VARIANT_POLL_MS));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Preview ready."));

    expect(screen.getByAltText("Original: A guitarist mid-solo")).toBeInTheDocument();
    const preview = screen.getByAltText("Personalised preview with Me: A guitarist mid-solo");
    expect(preview).toHaveAttribute(
      "src",
      "/api/gcs?bucket=b&path=" + encodeURIComponent(DONE.gcs_uri.replace("gs://b/", "")),
    );
    expect(screen.getByText("issues")).toBeInTheDocument();
    expect(screen.getByText("person likeness lost")).toBeInTheDocument();

    // Done: polling stopped.
    const calls = listVariants.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(VARIANT_POLL_MS * 3));
    expect(listVariants.mock.calls.length).toBe(calls);
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    listVariants.mockResolvedValue({ [CONCEPT]: { k1: { ...DONE, status: "rendering" } } });
    const { unmount } = renderPanel();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Rendering/));
    unmount();
    const calls = listVariants.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(VARIANT_POLL_MS * 3));
    expect(listVariants.mock.calls.length).toBe(calls);
  });

  it("stops polling after repeated failures and offers Retry", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    listVariants.mockResolvedValueOnce({ [CONCEPT]: { k1: { ...DONE, status: "rendering" } } });
    renderPanel();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Rendering/));

    // Half the polls fail, half lose the record: each counts as a miss.
    listVariants.mockImplementation(async () => {
      if (listVariants.mock.calls.length % 2) throw new Error("network");
      return {};
    });
    for (let i = 0; i < MAX_POLL_MISSES; i++) {
      await act(() => vi.advanceTimersByTimeAsync(VARIANT_POLL_MS));
    }
    expect(await screen.findByRole("alert")).toHaveTextContent(/stopped checking/);
    const calls = listVariants.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(VARIANT_POLL_MS * 3));
    expect(listVariants.mock.calls.length).toBe(calls);

    // Retry resumes polling and picks up the finished preview.
    listVariants.mockReset();
    listVariants.mockResolvedValue({ [CONCEPT]: { k1: DONE } });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Preview ready."));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows an earlier preview for the chosen person and clears it on a switch", async () => {
    listVariants.mockResolvedValue({ [CONCEPT]: { k1: DONE } });
    renderPanel();
    expect(await screen.findByAltText(/Personalised preview with Me/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Person" }), {
      target: { value: "consent-5678" },
    });
    await waitFor(() => expect(screen.queryByAltText(/Personalised preview/)).not.toBeInTheDocument());
  });

  it("shows the api's refusal as an alert", async () => {
    createVariant.mockRejectedValue(new VariantError("variant_cap_reached", 429));
    renderPanel();
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Person" })).toHaveValue("consent-1234"));
    fireEvent.click(screen.getByRole("button", { name: "Render preview" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/today's limit/);
  });

  it("links to /people when no one is registered", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: null });
    renderPanel();
    expect(await screen.findByRole("link", { name: "Register a person" })).toHaveAttribute("href", "/people");
    expect(screen.queryByRole("button", { name: "Render preview" })).not.toBeInTheDocument();
  });
});

describe("variants helpers", () => {
  it("parses the GET body, dropping malformed records", () => {
    const parsed = parseVariants({
      variants: { [CONCEPT]: { k1: DONE, bad: { status: "weird" }, worse: 3 }, other: "x" },
    });
    expect(parsed).toEqual({ [CONCEPT]: { k1: DONE } });
    expect(parseVariants(null)).toEqual({});
  });

  it("picks the newest variant for a person", () => {
    const older = { ...DONE, created_at: "2026-10-09T00:00:00Z" };
    const other = { ...DONE, consent_id: "consent-5678", created_at: "2026-10-11T00:00:00Z" };
    const map = { [CONCEPT]: { old: older, new: DONE, other } };
    expect(latestVariant(map, CONCEPT, "consent-1234")?.key).toBe("new");
    expect(latestVariant(map, CONCEPT, "nobody")).toBeNull();
    expect(latestVariant(map, "missing", "consent-1234")).toBeNull();
  });

  it("builds the owner-only proxy URL and image check only for finished previews", () => {
    expect(variantImageUrl(DONE)).toMatch(/^\/api\/gcs\?bucket=b&path=run%2Fcreative_output%2Fvariants%2F/);
    expect(variantImageUrl({ ...DONE, status: "rendering" })).toBeNull();
    expect(variantImageUrl(null)).toBeNull();
    expect(variantImageCheck(DONE)).toEqual({ passed: false, issues: ["person likeness lost"], rerenders: 1 });
    expect(variantImageCheck({ ...DONE, status: "failed" })).toBeUndefined();
  });

  it("offers Personalise only for rendered, uncast creatives", () => {
    const images = { [CONCEPT]: { gcs_uri: "gs://b/x.png" } };
    const proof = { concept: { concept_name: CONCEPT } };
    expect(canPersonalise(proof, images, false)).toBe(true);
    expect(canPersonalise({ ...proof, casting: { cast: false } }, images, false)).toBe(true);
    expect(canPersonalise({ ...proof, casting: { cast: true } }, images, false)).toBe(false);
    expect(canPersonalise(proof, images, true)).toBe(false);
    expect(canPersonalise(proof, {}, false)).toBe(false);
  });

  it("explains refusals", () => {
    expect(
      variantErrorMessage("concept_not_castable", 400, "can't feature a person: its style is not one of the person-safe styles"),
    ).toBe("This creative can't feature a person: its style is not one of the person-safe styles.");
    expect(variantErrorMessage("concept_already_cast", 400)).toMatch(/already features a person/);
    expect(variantErrorMessage(null, 500)).toBe("Something went wrong (500). Try again.");
  });
});
