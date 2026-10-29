// @vitest-environment jsdom
import React, { Suspense, act } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  findVariationFamilies,
  variationLineageLabel,
  type EvalReport,
  type Proof,
  type VisualConcept,
} from "@/lib/eval-matching";

const generateVariationsMock = vi.fn();
const getSessionMock = vi.fn();
const listArtifactsMock = vi.fn();
const getRatingsMock = vi.fn();
const fetchEvalReportMock = vi.fn();
let currentSearchParams = new URLSearchParams("app=creative_agent");

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    generateVariations: (...args: unknown[]) => generateVariationsMock(...args),
    getSession: (...args: unknown[]) => getSessionMock(...args),
    listArtifacts: (...args: unknown[]) => listArtifactsMock(...args),
    getRatings: (...args: unknown[]) => getRatingsMock(...args),
  };
});

vi.mock("@/lib/eval-report", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/eval-report")>()),
  fetchEvalReport: (...args: unknown[]) => fetchEvalReportMock(...args),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => currentSearchParams,
}));

import { VariationDrawer } from "@/app/results/[sessionId]/variation-drawer";
import { ProofGrid } from "@/app/results/[sessionId]/proof-grid";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";
import { DeployPanel } from "@/app/results/[sessionId]/deploy-panel";
import { ResultsHeader } from "@/app/results/[sessionId]/results-header";
import ResultsPage from "@/app/results/[sessionId]/page";

function makeConcept(overrides: Partial<VisualConcept> = {}): VisualConcept {
  return {
    ad_copy_id: 1,
    trend: "stadium guitar encore",
    aspect_ratio: "1:1",
    concept_name: "Stadium Encore 1",
    visual_style: "Photoreal / editorial",
    composition_mode: "product_in_scene",
    headline: "Built For Encore Sustain",
    social_caption: "PRS Custom 24 on tour.",
    call_to_action: "Play PRS Custom 24",
    concept_summary: "PRS Custom 24 on an illuminated stage riser.",
    image_generation_prompt: "Editorial stage photo of PRS Custom 24.",
    trend_reference: "Live stadium encore aesthetic.",
    markets_product: "Carved maple top under stage beams.",
    audience_appeal: "Touring guitarists.",
    selection_rationale: "High contrast hero frame.",
    ...overrides,
  };
}

function makeProof(index: number, overrides: Partial<VisualConcept> = {}): Proof {
  return {
    index,
    adCopyMatch: "id",
    concept: makeConcept({
      concept_name: `Stadium Encore ${index + 1}`,
      headline: `Encore Headline ${index + 1}`,
      ...overrides,
    }),
  };
}

beforeEach(() => {
  generateVariationsMock.mockReset();
  getSessionMock.mockReset();
  listArtifactsMock.mockReset();
  getRatingsMock.mockReset();
  fetchEvalReportMock.mockReset();
  listArtifactsMock.mockResolvedValue([]);
  getRatingsMock.mockResolvedValue([]);
  currentSearchParams = new URLSearchParams("app=creative_agent");
});

describe("variationLineageLabel & findVariationFamilies", () => {
  it("formats variation lineage cleanly and returns null for base creatives", () => {
    const base = makeConcept();
    expect(variationLineageLabel(base)).toBeNull();

    const variant = makeConcept({
      concept_name: "Stadium Encore 1 — Style Comic 1",
      variation_of: "Stadium Encore 1",
      variation_base_index: 0,
      variation_axis: "visual_style",
      variation_value: "Comic panel",
    });
    expect(variationLineageLabel(variant)).toBe(
      "Variant of Stadium Encore 1 · Visual Style: Comic panel"
    );
  });

  it("groups [base, ...variants] families and caps armIndices at 4 arms", () => {
    const proofs: Proof[] = [
      makeProof(0, { concept_name: "Base Hero" }),
      makeProof(1, { concept_name: "Other Base" }),
      makeProof(2, {
        concept_name: "Base Hero — Var 1",
        variation_of: "Base Hero",
        variation_base_index: 0,
        variation_axis: "cta_hook",
        variation_value: "Hook 1",
      }),
      makeProof(3, {
        concept_name: "Base Hero — Var 2",
        variation_of: "Base Hero",
        variation_base_index: 0,
        variation_axis: "cta_hook",
        variation_value: "Hook 2",
      }),
      makeProof(4, {
        concept_name: "Base Hero — Var 3",
        variation_of: "Base Hero",
        variation_base_index: 0,
        variation_axis: "cta_hook",
        variation_value: "Hook 3",
      }),
      makeProof(5, {
        concept_name: "Base Hero — Var 4",
        variation_of: "Base Hero",
        variation_base_index: 0,
        variation_axis: "cta_hook",
        variation_value: "Hook 4",
      }),
    ];

    const families = findVariationFamilies(proofs, 4);
    expect(families).toEqual([
      {
        baseIndex: 0,
        baseName: "Base Hero",
        armIndices: [0, 2, 3, 4],
      },
    ]);
  });
});

describe("VariationDrawer", () => {
  it("renders all 6 axes, updates frozen-invariant preview, and submits variation payload", async () => {
    const onClose = vi.fn();
    const onGenerated = vi.fn();
    const baseProof = makeProof(0);

    generateVariationsMock.mockResolvedValue({
      session_id: "sess-1",
      base_concept_index: 0,
      axis: "cta_hook",
      added_indices: [4, 5],
      variations: [],
      new_ad_copies: [],
      generated_images: {},
      creative_evaluation_report: null,
      warnings: [],
    });

    render(
      <VariationDrawer
        open
        proof={baseProof}
        appName="creative_agent"
        userId="alice@example.com"
        sessionId="sess-1"
        imageUrlFor={() => null}
        onClose={onClose}
        onGenerated={onGenerated}
      />
    );

    expect(
      screen.getByRole("heading", { name: "Generate controlled variations" })
    ).toBeInTheDocument();
    expect(screen.getByText("Encore Headline 1")).toBeInTheDocument();

    const axisGroup = screen.getByRole("radiogroup", { name: "Variation axis" });
    const radios = within(axisGroup).getAllByRole("radio");
    expect(radios).toHaveLength(6);

    // Default axis is visual_style
    const preview = screen.getByTestId("axis-invariant-preview");
    expect(preview).toHaveTextContent("Visual style family and image generation prompt");
    expect(preview).toHaveTextContent("All ad copy fields, composition mode");

    // Switch to Composition Mode -> renders chips and disables base mode (product_in_scene)
    fireEvent.click(screen.getByRole("radio", { name: "Composition Mode" }));
    const baseChip = screen.getByRole("button", { name: "product_in_scene (base)" });
    expect(baseChip).toBeDisabled();
    const heroChip = screen.getByRole("button", { name: "product_hero" });
    expect(heroChip).toBeEnabled();

    // Switch to CTA / Hook -> updates invariant preview and shows text input
    fireEvent.click(screen.getByRole("radio", { name: "CTA / Hook" }));
    expect(preview).toHaveTextContent("Opening headline hook and call-to-action phrase");
    expect(preview).toHaveTextContent("Body copy, social caption, tone, visual style");

    const input = screen.getByLabelText("Target values (optional)");
    fireEvent.change(input, {
      target: { value: "Urgency hook; Curiosity question hook" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Generate 2 variations" }));

    await waitFor(() => expect(generateVariationsMock).toHaveBeenCalledTimes(1));
    expect(generateVariationsMock).toHaveBeenCalledWith(
      "creative_agent",
      "alice@example.com",
      "sess-1",
      {
        base_concept_index: 0,
        axis: "cta_hook",
        count: 2,
        target_values: ["Urgency hook", "Curiosity question hook"],
      },
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
    await waitFor(() => expect(onGenerated).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("surfaces backend errors via role=alert without closing the dialog", async () => {
    const onClose = vi.fn();
    const onGenerated = vi.fn();
    generateVariationsMock.mockRejectedValue(new Error("Session reached 16 creatives"));

    render(
      <VariationDrawer
        open
        proof={makeProof(1)}
        appName="creative_agent"
        userId="alice@example.com"
        sessionId="sess-1"
        imageUrlFor={() => null}
        onClose={onClose}
        onGenerated={onGenerated}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Generate 2 variations" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Session reached 16 creatives");
    expect(onGenerated).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("ProofGrid & ProofDetail variation lineage and actions", () => {
  const baseProof = makeProof(0, { concept_name: "Stadium Encore 1" });
  const variantProof = makeProof(1, {
    concept_name: "Stadium Encore 1 — Locale Tokyo 1",
    variation_of: "Stadium Encore 1",
    variation_base_index: 0,
    variation_axis: "locale_market",
    variation_value: "Tokyo, Japan",
  });

  it("renders lineage badges only on variation proofs and avoids nested buttons in ProofGrid", () => {
    const onOpenVariations = vi.fn();
    const { container } = render(
      <ProofGrid
        proofs={[baseProof, variantProof]}
        sort="pipeline"
        onSortChange={() => {}}
        imageUrlFor={() => null}
        onOpen={() => {}}
        itemRef={() => () => {}}
        onOpenVariations={onOpenVariations}
      />
    );

    // Accessibility check: zero nested <button> elements inside <button>.
    expect(container.querySelectorAll("button button")).toHaveLength(0);

    const badges = screen.getAllByTestId("variation-lineage-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveTextContent(
      "Variant of Stadium Encore 1 · Locale / Market: Tokyo, Japan"
    );

    const triggers = screen.getAllByRole("button", { name: "Controlled variations" });
    expect(triggers).toHaveLength(2);
    fireEvent.click(triggers[0]);
    expect(onOpenVariations).toHaveBeenCalledWith(baseProof);
  });

  it("renders lineage badge and variation trigger inside ProofDetail", () => {
    const onOpenVariations = vi.fn();
    render(
      <ProofDetail
        proofs={[baseProof, variantProof]}
        open
        index={1}
        onIndexChange={() => {}}
        onClose={() => {}}
        imageUrlFor={() => null}
        returnFocusTo={() => null}
        onOpenVariations={onOpenVariations}
      />
    );

    expect(screen.getByTestId("detail-variation-lineage")).toHaveTextContent(
      "Variant of Stadium Encore 1 · Locale / Market: Tokyo, Japan"
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Generate controlled variations" })
    );
    expect(onOpenVariations).toHaveBeenCalledWith(variantProof);
  });
});

describe("DeployPanel & ResultsHeader", () => {
  it("hides Select Base + Variants when no variations exist and selects <=4 family arms when present", () => {
    const baseOnly = [makeProof(0), makeProof(1)];
    const { rerender } = render(
      <DeployPanel
        proofs={baseOnly}
        appName="creative_agent"
        sessionId="sess-1"
        imageUrlFor={() => null}
        stoppedEarly={false}
      />
    );

    expect(
      screen.queryByRole("button", { name: /Select Base \+ Variants/ })
    ).not.toBeInTheDocument();

    const withFamily = [
      makeProof(0, { concept_name: "Stadium Encore 1" }),
      makeProof(1, { concept_name: "Stadium Encore 2" }),
      makeProof(2, {
        concept_name: "Stadium Encore 1 — Style 1",
        variation_of: "Stadium Encore 1",
        variation_base_index: 0,
        variation_axis: "visual_style",
        variation_value: "Film",
      }),
      makeProof(3, {
        concept_name: "Stadium Encore 1 — Style 2",
        variation_of: "Stadium Encore 1",
        variation_base_index: 0,
        variation_axis: "visual_style",
        variation_value: "Vector",
      }),
      makeProof(4, {
        concept_name: "Stadium Encore 1 — Style 3",
        variation_of: "Stadium Encore 1",
        variation_base_index: 0,
        variation_axis: "visual_style",
        variation_value: "3D Clay",
      }),
      makeProof(5, {
        concept_name: "Stadium Encore 1 — Style 4",
        variation_of: "Stadium Encore 1",
        variation_base_index: 0,
        variation_axis: "visual_style",
        variation_value: "Risograph",
      }),
    ];

    rerender(
      <DeployPanel
        proofs={withFamily}
        appName="creative_agent"
        sessionId="sess-1"
        imageUrlFor={() => null}
        stoppedEarly={false}
      />
    );

    expect(screen.getAllByTestId("deploy-variation-lineage")).toHaveLength(4);
    const presetBtn = screen.getByRole("button", { name: "Select Base + Variants" });
    fireEvent.click(presetBtn);

    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    const checkedIndices = checkboxes
      .map((cb, idx) => (cb.checked ? idx : -1))
      .filter((idx) => idx >= 0);
    // Selects base (0) + first 3 variants (2, 3, 4) = 4 arms max!
    expect(checkedIndices).toEqual([0, 2, 3, 4]);
  });

  it("renders Download Delivery Kit (.zip) link when deliveryKitUrl is provided", () => {
    const { rerender } = render(
      <ResultsHeader
        appName="creative_agent"
        sessionId="sess-1"
        summary="PRS Guitars · PRS Custom 24"
        campaignFields={[]}
        visualDirectionFields={[]}
        gcsUri=""
        galleryUrl={null}
        deliveryKitUrl={null}
      />
    );
    expect(
      screen.queryByRole("link", { name: "Download Delivery Kit (.zip)" })
    ).not.toBeInTheDocument();

    rerender(
      <ResultsHeader
        appName="creative_agent"
        sessionId="sess-1"
        summary="PRS Guitars · PRS Custom 24"
        campaignFields={[]}
        visualDirectionFields={[]}
        gcsUri=""
        galleryUrl={null}
        deliveryKitUrl="/api/adk/runs/creative_agent/alice/sess-1/delivery-kit"
      />
    );

    const link = screen.getByRole("link", { name: "Download Delivery Kit (.zip)" });
    expect(link).toHaveAttribute(
      "href",
      "/api/adk/runs/creative_agent/alice/sess-1/delivery-kit"
    );
    expect(link).toHaveAttribute("download");
  });

  it("clamps effectiveCount and disables COUNT_OPTIONS exceeding remainingSlots in VariationDrawer", () => {
    render(
      <VariationDrawer
        open
        proof={makeProof(0)}
        appName="creative_agent"
        userId="alice@example.com"
        sessionId="sess-1"
        imageUrlFor={() => null}
        remainingSlots={1}
        onClose={() => {}}
        onGenerated={() => {}}
      />
    );

    const opt1 = screen.getByRole("button", { name: "1 variant" });
    const opt2 = screen.getByRole("button", { name: "2 variants" });
    const opt3 = screen.getByRole("button", { name: "3 variants" });

    expect(opt1).toBeEnabled();
    expect(opt1).toHaveAttribute("aria-pressed", "true");
    expect(opt2).toBeDisabled();
    expect(opt3).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Generate 1 variation" })
    ).toBeEnabled();
  });
});

function makeEvalReport(conceptNames: string[]): EvalReport {
  return {
    brand: "PRS Guitars",
    target_product: "PRS Custom 24",
    target_search_trend: "stadium guitar encore",
    ad_copy_evaluations: [
      {
        original_id: 1,
        headline: "Built For Encore Sustain",
        tone_style: "Aspirational",
        score: {
          overall_score: 0.85,
          passed: true,
          verdicts: [],
          strengths: [],
          improvements: [],
        },
      },
    ],
    visual_concept_evaluations: conceptNames.map((name, i) => ({
      ad_copy_id: 1,
      concept_name: name,
      score: {
        overall_score: 0.9 - i * 0.02,
        passed: true,
        verdicts: [],
        strengths: [],
        improvements: [],
      },
    })),
    summary: {
      total_ad_copies: 1,
      ad_copies_passed: 1,
      avg_ad_copy_score: 0.85,
      total_visual_concepts: conceptNames.length,
      visual_concepts_passed: conceptNames.length,
      avg_visual_score: 0.88,
      overall_pass_rate: 1.0,
      weakest_dimensions: [],
      gates_pass_rate: 1.0,
    },
  };
}

describe("ResultsPage loadEval state fallback & GCS staleness guard", () => {
  it("renders sess.state.creative_evaluation_report on load when GCS keys are absent", async () => {
    const concepts = [
      makeConcept({ concept_name: "Base 1", headline: "Base Headline" }),
      makeConcept({
        concept_name: "Base 1 — Style Film 1",
        headline: "Variant Headline",
        variation_of: "Base 1",
        variation_base_index: 0,
        variation_axis: "visual_style",
        variation_value: "Film",
      }),
    ];
    const stateReport = makeEvalReport(["Base 1", "Base 1 — Style Film 1"]);

    getSessionMock.mockResolvedValue({
      id: "sess-local-mem",
      appName: "creative_agent",
      userId: "me",
      events: [{ author: "creative_agent", content: { parts: [{ text: "Done" }] } }],
      state: {
        brand: "PRS Guitars",
        target_product: "PRS Custom 24",
        finalize_done: true,
        final_visual_concepts: { visual_concepts: concepts },
        ad_copy_critique: { ad_copies: [] },
        generated_images: {
          "Base 1": { gcs_uri: "", artifact_key: "base_1.png" },
          "Base 1 — Style Film 1": {
            gcs_uri: "",
            artifact_key: "base_1_style_film_1.png",
          },
        },
        creative_evaluation_report: stateReport,
      },
    });

    const paramsPromise = Promise.resolve({ sessionId: "sess-local-mem" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Variant Headline" })
      ).toBeInTheDocument();
    });
    expect(fetchEvalReportMock).not.toHaveBeenCalled();
    // Variant card renders its 88% visual judge score from stateReport!
    expect(screen.getAllByText("88").length).toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText("1 of 1 ad copy and 2 of 2 visuals pass.")
    ).toBeInTheDocument();

    // Open DeployDisclosure to verify DeployPanel also receives the hydrated eval scores
    fireEvent.click(
      screen.getByRole("button", { name: /Deploy creatives as a live experiment/i })
    );
    expect(await screen.findByText("Score 87%")).toBeInTheDocument();
  });

  it("hydrates stateReport inside loadEval when eval_report_gcs_uri is set without GCS folder keys", async () => {
    const concepts = [makeConcept({ concept_name: "Base 1", headline: "Solo Base" })];
    const stateReport = makeEvalReport(["Base 1"]);

    getSessionMock.mockResolvedValue({
      id: "sess-load-eval-no-gcs",
      appName: "creative_agent",
      userId: "me",
      events: [{ author: "creative_agent", content: { parts: [{ text: "Done" }] } }],
      state: {
        brand: "PRS Guitars",
        eval_report_gcs_uri: "gs://some-bucket/report.json",
        final_visual_concepts: { visual_concepts: concepts },
        ad_copy_critique: { ad_copies: [] },
        creative_evaluation_report: JSON.stringify(stateReport),
      },
    });

    const paramsPromise = Promise.resolve({ sessionId: "sess-load-eval-no-gcs" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Solo Base" })).toBeInTheDocument();
    });
    expect(fetchEvalReportMock).not.toHaveBeenCalled();
    expect(screen.getAllByText("90").length).toBeGreaterThanOrEqual(1);
  });

  it("appends cache-busting &v=<count> and prefers fresher stateReport over stale GCS report", async () => {
    const concepts = [
      makeConcept({ concept_name: "Base 1", headline: "Base Headline" }),
      makeConcept({
        concept_name: "Base 1 — Style Film 1",
        headline: "Variant Headline",
        variation_of: "Base 1",
        variation_base_index: 0,
        variation_axis: "visual_style",
        variation_value: "Film",
      }),
    ];
    const staleGcsReport = makeEvalReport(["Base 1"]);
    const freshStateReport = makeEvalReport(["Base 1", "Base 1 — Style Film 1"]);

    getSessionMock.mockResolvedValue({
      id: "sess-stale-gcs",
      appName: "creative_agent",
      userId: "me",
      events: [{ author: "creative_agent", content: { parts: [{ text: "Done" }] } }],
      state: {
        brand: "PRS Guitars",
        target_product: "PRS Custom 24",
        gcs_bucket_name: "allowed-bucket",
        gcs_folder: "out",
        agent_output_dir: "run1",
        eval_report_gcs_uri: "gs://allowed-bucket/out/run1/creative_eval_report.json",
        _images_generated: true,
        finalize_done: true,
        final_visual_concepts: { visual_concepts: concepts },
        ad_copy_critique: { ad_copies: [] },
        generated_images: {
          "Base 1": { gcs_uri: "gs://allowed-bucket/out/run1/base_1.png" },
          "Base 1 — Style Film 1": {
            gcs_uri: "gs://allowed-bucket/out/run1/base_1_style_film_1.png",
          },
        },
        creative_variations: [
          {
            index: 1,
            concept_name: "Base 1 — Style Film 1",
            variation_of: "Base 1",
            variation_base_index: 0,
            axis: "visual_style",
            value: "Film",
          },
        ],
        creative_evaluation_report: freshStateReport,
      },
    });
    fetchEvalReportMock.mockResolvedValue({
      status: "found",
      report: staleGcsReport,
    });

    const paramsPromise = Promise.resolve({ sessionId: "sess-stale-gcs" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(fetchEvalReportMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchEvalReportMock).toHaveBeenCalledWith(
      expect.stringContaining("&v=1")
    );
    // Variant visual score (0.88 -> 88) & 2-visual summary are rendered because fresher stateReport won over stale 1-concept GCS report!
    await waitFor(() => {
      expect(
        screen.getByText("1 of 1 ad copy and 2 of 2 visuals pass.")
      ).toBeInTheDocument();
      expect(screen.getAllByText("88").length).toBeGreaterThanOrEqual(1);
    });
  });
});
