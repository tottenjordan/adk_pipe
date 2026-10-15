// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A stable object per test: the form's prefill effect depends on its identity.
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useSearchParams: () => search,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import Home from "@/app/page";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";
import { ProofGrid } from "@/app/results/[sessionId]/proof-grid";
import { BriefEditForm } from "@/app/run/[sessionId]/review-brief";
import { SaferAngleLine } from "@/components/safer-angle-line";
import { buildBriefEdit, toBriefPayload } from "@/lib/brief-edit";
import { parseCreativeBrief, type CreativeBrief } from "@/lib/creative-brief";
import { buildProofs, namesRealPersonFor, type AdCopy, type VisualConcept } from "@/lib/eval-matching";
import { buildInitialState } from "@/lib/initial-state";
import { briefFromState } from "@/lib/run-history";
import { namedCopyIds, saferAngleText } from "@/lib/safer-angle";
import { buildDisplayFields, SAFER_ANGLE_FIELD } from "@/lib/utils";
import type { CampaignInput } from "@/lib/types";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

const SENSITIVE = JSON.stringify({ context: "c", sensitive: true, sensitive_reason: "death of a real person" });
const PLAIN = JSON.stringify({ context: "c", hook: "h" });

const params = (agent: string, trendContext: string) =>
  new URLSearchParams({ agent, brand: "Hohner", targetSearchTrend: "Tupac", trendContext });

describe("campaign form: respectful angle", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("offers the opt-in inside the sensitive alert, off by default, and swaps the advice when checked", async () => {
    search = params("creative_agent", SENSITIVE);
    render(<Home />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Creatives will never reference the event");
    const box = screen.getByRole("checkbox", { name: "Look for a respectful angle" });
    expect(box).not.toBeChecked();
    expect(box).toHaveAccessibleDescription(
      "Builds on the positive side of the trend, like a person's legacy. The event itself is never referenced, and a named person appears only as a respectful tribute, never as an endorsement.",
    );
    fireEvent.click(box);
    expect(box).toBeChecked();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Sensitive trend: death of a real person. Creatives will look for a respectful angle; the event is never referenced.",
    );
  });

  it("is not offered for a non-sensitive trend or for trend_scout", async () => {
    search = params("creative_agent", PLAIN);
    const { unmount } = render(<Home />);
    await screen.findByText(/Pre-filled with trend/);
    expect(screen.queryByRole("checkbox", { name: "Look for a respectful angle" })).not.toBeInTheDocument();
    unmount();

    search = params("trend_scout", SENSITIVE);
    render(<Home />);
    await screen.findByText(/Pre-filled with trend/);
    expect(screen.queryByRole("checkbox", { name: "Look for a respectful angle" })).not.toBeInTheDocument();
  });

  it("is cleared when the trend is edited", async () => {
    search = params("interactive_creative", SENSITIVE);
    render(<Home />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Look for a respectful angle" }));
    fireEvent.change(screen.getByLabelText("Target search trend"), { target: { value: "World Series" } });
    expect(screen.queryByRole("checkbox", { name: "Look for a respectful angle" })).not.toBeInTheDocument();
  });
});

describe("buildInitialState: safer_angle_opt_in", () => {
  const base: CampaignInput = {
    agent: "creative_agent",
    brand: "Hohner",
    targetAudience: "a",
    targetProduct: "Clavinet D7",
    keySellingPoints: "k",
    targetSearchTrend: "Tupac",
  };

  it("is seeded only when checked AND the carried context is sensitive (creative agents)", () => {
    for (const agent of ["creative_agent", "interactive_creative"] as const) {
      expect(
        buildInitialState({ ...base, agent, saferAngle: true, trendContext: SENSITIVE }).safer_angle_opt_in,
      ).toBe(true);
    }
    expect(buildInitialState({ ...base, saferAngle: true, trendContext: PLAIN })).not.toHaveProperty(
      "safer_angle_opt_in",
    );
    expect(buildInitialState({ ...base, saferAngle: true })).not.toHaveProperty("safer_angle_opt_in");
    expect(buildInitialState({ ...base, saferAngle: false, trendContext: SENSITIVE })).not.toHaveProperty(
      "safer_angle_opt_in",
    );
    expect(
      buildInitialState({ ...base, agent: "trend_scout", saferAngle: true, trendContext: SENSITIVE }),
    ).not.toHaveProperty("safer_angle_opt_in");
  });
});

describe("Duplicate brief: safer angle", () => {
  it("restores the opt-in only with a sensitive context", () => {
    const state = { ui_app: "creative_agent", safer_angle_opt_in: true, trend_scout_context: SENSITIVE };
    expect(briefFromState(state).saferAngle).toBe(true);
    expect(briefFromState({ ...state, trend_scout_context: PLAIN }).saferAngle).toBeUndefined();
    expect(briefFromState({ ...state, safer_angle_opt_in: "true" }).saferAngle).toBeUndefined();
  });
});

describe("Safer angle metadata row", () => {
  it('reads "On" for an opted-in or active run, and is hidden otherwise', () => {
    expect(buildDisplayFields({ safer_angle_opt_in: true }, [SAFER_ANGLE_FIELD])).toEqual([
      { label: "Safer angle", key: "safer_angle_opt_in", value: "On" },
    ]);
    expect(buildDisplayFields({ safer_angle_active: "yes" }, [SAFER_ANGLE_FIELD])[0]?.value).toBe("On");
    expect(buildDisplayFields({}, [SAFER_ANGLE_FIELD])).toEqual([]);
    expect(buildDisplayFields({ safer_angle_active: "" }, [SAFER_ANGLE_FIELD])).toEqual([]);
  });
});

const briefWith = (saferAngle?: string) => {
  const raw = structuredClone(SAMPLE_CREATIVE_BRIEF) as Record<string, unknown>;
  if (saferAngle !== undefined) {
    raw.trend_bridge = { ...(raw.trend_bridge as object), safer_angle: saferAngle };
  }
  return raw;
};

describe("SaferAngleLine", () => {
  it("shows the brief's safer angle", () => {
    const state = { safer_angle_active: "yes", creative_brief: briefWith("Tupac's legacy of chasing your music dreams") };
    render(<SaferAngleLine state={state} />);
    expect(screen.getByText(/Safer angle:/).closest("p")).toHaveTextContent(
      "Safer angle: Tupac's legacy of chasing your music dreams",
    );
  });

  it("shows the fallback when active with a brief but no angle (also from a JSON string)", () => {
    render(<SaferAngleLine state={{ safer_angle_active: "yes", creative_brief: JSON.stringify(briefWith("")) }} />);
    expect(screen.getByText(/Safer angle:/).closest("p")).toHaveTextContent(
      "Safer angle: none found — the event is never referenced",
    );
  });

  it("renders nothing when inactive or before the brief exists", () => {
    const { container } = render(<SaferAngleLine state={{ creative_brief: briefWith("An angle") }} />);
    expect(container).toBeEmptyDOMElement();
    expect(saferAngleText({ safer_angle_active: "yes" })).toBe("");
    expect(saferAngleText({ safer_angle_active: "no", creative_brief: briefWith("x") })).toBe("");
  });
});

const concept = (id: number, name = `Concept ${id}`) =>
  ({ concept_name: name, headline: `Headline ${id}`, ad_copy_id: id, concept_summary: "s" }) as VisualConcept;
const copy = (id: number) => ({ original_id: id, headline: `Headline ${id}` }) as AdCopy;

describe("Names a real person", () => {
  it("pairs named copy ids with proofs (numbers or strings; never a positional guess)", () => {
    const proofs = buildProofs([concept(1), concept(2)], [copy(1), copy(2)], null, undefined, undefined, undefined, 0, [
      "2",
    ]);
    expect(proofs.map((p) => p.namesRealPerson)).toEqual([undefined, true]);
    expect(namedCopyIds([1, " 3 ", null, "", Number.NaN])).toEqual(new Set(["1", "3"]));
    const named = new Set(["7"]);
    expect(namesRealPersonFor({ ad_copy_id: 9 }, { item: { original_id: 7 }, via: "index" }, named)).toBe(false);
    expect(namesRealPersonFor({ ad_copy_id: 9 }, { item: { original_id: 7 }, via: "headline" }, named)).toBe(true);
    expect(buildProofs([concept(1)], [copy(1)], null)[0].namesRealPerson).toBeUndefined();
  });

  it("shows a mark-pending chip on the proof and the tribute note in the detail", () => {
    const proofs = buildProofs([concept(1), concept(2)], [copy(1), copy(2)], null, undefined, undefined, undefined, 0, [
      1,
    ]);
    render(
      <ProofGrid proofs={proofs} sort="pipeline" onSortChange={() => {}} imageUrlFor={() => null} onOpen={() => {}} itemRef={() => () => {}} />,
    );
    const chips = screen.getAllByText("Names a real person");
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveClass("text-mark-pending");
    expect(screen.getByRole("button", { name: /Open creative 1: .* · names a real person/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open creative 2: Headline 2" })).toBeInTheDocument();
  });

  it("adds the rights note to the proof detail", () => {
    const proofs = buildProofs([concept(1)], [copy(1)], null, undefined, undefined, undefined, 0, [1]);
    render(
      <ProofDetail
        proofs={proofs}
        open
        index={0}
        onIndexChange={() => {}}
        onClose={() => {}}
        imageUrlFor={() => null}
        returnFocusTo={() => null}
      />,
    );
    expect(
      screen.getByText("Respectful tribute only. Check name and likeness rights before using this live."),
    ).toBeInTheDocument();
  });
});

describe("brief edit: safer angle", () => {
  it("round-trips trend_bridge.safer_angle and keeps old briefs without it unchanged", () => {
    const withAngle = parseCreativeBrief(briefWith("A legacy angle"))!;
    expect(withAngle.trendBridge.saferAngle).toBe("A legacy angle");
    expect(toBriefPayload(withAngle)).toEqual(briefWith("A legacy angle"));

    const old = parseCreativeBrief(SAMPLE_CREATIVE_BRIEF)!;
    expect(old.trendBridge.saferAngle).toBe("");
    expect(toBriefPayload(old)).toEqual(SAMPLE_CREATIVE_BRIEF);
    expect(buildBriefEdit(old, structuredClone(old))).toBeNull();

    const edited = structuredClone(old);
    edited.trendBridge.saferAngle = "  New angle ";
    const edit = buildBriefEdit(old, edited)!;
    expect((edit[0].value.trend_bridge as Record<string, unknown>).safer_angle).toBe("New angle");
  });

  it("shows the optional field only when asked, and edits it", () => {
    const draft = parseCreativeBrief(briefWith("A legacy angle"))!;
    const onChange = vi.fn<(d: CreativeBrief) => void>();
    const { rerender } = render(<BriefEditForm draft={draft} onChange={onChange} errors={{}} />);
    expect(screen.queryByLabelText("Safer angle (optional)")).not.toBeInTheDocument();
    rerender(<BriefEditForm draft={draft} onChange={onChange} errors={{}} showSaferAngle />);
    const field = screen.getByLabelText("Safer angle (optional)");
    expect(field).toHaveValue("A legacy angle");
    fireEvent.change(field, { target: { value: "Another angle" } });
    expect(onChange.mock.calls.at(-1)?.[0].trendBridge.saferAngle).toBe("Another angle");
  });
});
