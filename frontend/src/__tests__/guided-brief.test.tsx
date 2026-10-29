// @vitest-environment jsdom
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BrandAssistSuggestion } from "@/lib/types";

const pushMock = vi.fn();
let currentSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  useSearchParams: () => currentSearchParams,
}));

const listSessionsMock = vi.fn();
const listPersonRefsMock = vi.fn();
const createSessionMock = vi.fn();
const fetchBrandAssistMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listSessions: (...a: unknown[]) => listSessionsMock(...a),
    listPersonRefs: (...a: unknown[]) => listPersonRefsMock(...a),
    createSession: (...a: unknown[]) => createSessionMock(...a),
    fetchBrandAssist: (...a: unknown[]) => fetchBrandAssistMock(...a),
  };
});

import Home from "@/app/page";
import { formatPaletteForBrief } from "@/components/guided-brief-stepper";
import {
  PipelineInspector,
  extractResiduals,
  extractSearchQueries,
  extractSourceItems,
} from "@/app/run/[sessionId]/pipeline-inspector";

const SAMPLE_ASSIST: BrandAssistSuggestion = {
  suggested_products: ["PRS SE CE24", "PRS Silver Sky", "PRS Custom 24"],
  target_audience: "Gigging guitarists and home-studio tone seekers.",
  key_selling_points: "Bolt-on maple neck snap; 85/15 'S' coil-split versatility",
  brand_palette_hex: ["#1A1A1A", "#F5F2EB", "#C9A227"],
  tone_summary: "Warm stage-lit editorial photography highlighting figured maple grain.",
  brand_cues: ["bird fret inlays", "figured maple top"],
};

beforeEach(() => {
  currentSearchParams = new URLSearchParams();
  sessionStorage.clear();
  pushMock.mockReset();
  listSessionsMock.mockReset();
  listPersonRefsMock.mockReset();
  createSessionMock.mockReset();
  fetchBrandAssistMock.mockReset();
  listSessionsMock.mockResolvedValue([]);
  listPersonRefsMock.mockResolvedValue({ personRefs: [], prefix: null });
});

describe("Home intake mode: Quick form (default) ↔ Guided brief", () => {
  it("renders Quick form by default with complete WAI-ARIA tablist/tabpanel semantics and ArrowLeft/ArrowRight navigation", async () => {
    render(<Home />);
    await waitFor(() => expect(listSessionsMock).toHaveBeenCalled());

    const quickTab = screen.getByRole("tab", { name: "Quick form" });
    const guidedTab = screen.getByRole("tab", { name: "Guided brief" });
    const panel = screen.getByRole("tabpanel");

    expect(quickTab.id).toBe("tab-quick-form");
    expect(guidedTab.id).toBe("tab-guided-brief");
    expect(quickTab.getAttribute("aria-controls")).toBe("brief-intake-panel");
    expect(guidedTab.getAttribute("aria-controls")).toBe("brief-intake-panel");
    expect(quickTab.getAttribute("aria-selected")).toBe("true");
    expect(guidedTab.getAttribute("aria-selected")).toBe("false");
    expect(quickTab.tabIndex).toBe(0);
    expect(guidedTab.tabIndex).toBe(-1);
    expect(panel.id).toBe("brief-intake-panel");
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-quick-form");

    // All core Quick Form inputs are immediately visible without step navigation
    expect(screen.getByLabelText("Brand name")).toBeTruthy();
    expect(screen.getByLabelText("Target product")).toBeTruthy();
    expect(screen.getByLabelText("Target audience")).toBeTruthy();
    expect(screen.getByLabelText("Key selling points")).toBeTruthy();
    expect(screen.getByLabelText("Country")).toBeTruthy();
    expect(screen.getByLabelText("Region or metro (optional)")).toBeTruthy();

    // ArrowRight switches to Guided brief and moves focus to #tab-guided-brief
    quickTab.focus();
    fireEvent.keyDown(quickTab, { key: "ArrowRight" });
    expect(guidedTab.getAttribute("aria-selected")).toBe("true");
    expect(guidedTab.tabIndex).toBe(0);
    expect(quickTab.tabIndex).toBe(-1);
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-guided-brief");
    expect(document.activeElement).toBe(guidedTab);

    // ArrowLeft switches back to Quick form and moves focus to #tab-quick-form
    fireEvent.keyDown(guidedTab, { key: "ArrowLeft" });
    expect(quickTab.getAttribute("aria-selected")).toBe("true");
    expect(quickTab.tabIndex).toBe(0);
    expect(guidedTab.tabIndex).toBe(-1);
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-quick-form");
    expect(document.activeElement).toBe(quickTab);
  });

  it("round-trips all campaign, regional, and grounding fields losslessly between Quick form and Guided brief", async () => {
    render(<Home />);
    await waitFor(() => expect(listSessionsMock).toHaveBeenCalled());

    // 1. Fill fields in Quick Form (starting in trend_scout)
    fireEvent.change(screen.getByLabelText("Brand name"), {
      target: { value: "PRS Guitars" },
    });
    fireEvent.change(screen.getByLabelText("Target product"), {
      target: { value: "SE CE24" },
    });
    fireEvent.change(screen.getByLabelText("Target audience"), {
      target: { value: "Indie rock guitarists" },
    });
    fireEvent.change(screen.getByLabelText("Key selling points"), {
      target: { value: "Coil-split versatility" },
    });
    fireEvent.change(screen.getByLabelText("Country"), {
      target: { value: "JP" },
    });
    fireEvent.change(screen.getByLabelText("Region or metro (optional)"), {
      target: { value: "Tokyo" },
    });

    // 2. Switch to Guided brief — Step 1 preserves Brand & Product
    fireEvent.click(screen.getByRole("tab", { name: "Guided brief" }));
    expect(
      (screen.getByLabelText("Brand name") as HTMLInputElement).value,
    ).toBe("PRS Guitars");
    expect(
      (screen.getByLabelText("Target product") as HTMLInputElement).value,
    ).toBe("SE CE24");

    // Step 2 preserves Country + Region, and lets us switch agent to creative_agent
    fireEvent.click(screen.getByRole("button", { name: "2. Trend & Market" }));
    expect((screen.getByLabelText("Country") as HTMLSelectElement).value).toBe(
      "JP",
    );
    expect(
      (screen.getByLabelText("Region or metro (optional)") as HTMLInputElement)
        .value,
    ).toBe("Tokyo");

    const creativeRadio = document.querySelector(
      'input[name="agent"][value="creative_agent"]',
    ) as HTMLInputElement;
    fireEvent.click(creativeRadio);
    fireEvent.change(screen.getByLabelText("Target search trend"), {
      target: { value: "tiny desk concert" },
    });

    // Step 3 preserves Audience & Selling points and lets us toggle Enterprise grounding
    fireEvent.click(
      screen.getByRole("button", { name: "3. Audience & Launch" }),
    );
    expect(
      (screen.getByLabelText("Target audience") as HTMLTextAreaElement).value,
    ).toBe("Indie rock guitarists");
    expect(
      (screen.getByLabelText("Key selling points") as HTMLTextAreaElement).value,
    ).toBe("Coil-split versatility");

    const enterpriseCheckbox = screen.getByLabelText(
      /Ground in Enterprise Knowledge Base/i,
    ) as HTMLInputElement;
    expect(enterpriseCheckbox.checked).toBe(false);
    fireEvent.click(enterpriseCheckbox);
    expect(enterpriseCheckbox.checked).toBe(true);

    // 3. Switch back to Quick form — every single edit made in Guided brief is preserved!
    fireEvent.click(screen.getByRole("tab", { name: "Quick form" }));
    expect(
      (screen.getByLabelText("Brand name") as HTMLInputElement).value,
    ).toBe("PRS Guitars");
    expect(
      (screen.getByLabelText("Target product") as HTMLInputElement).value,
    ).toBe("SE CE24");
    expect(
      (screen.getByLabelText("Target audience") as HTMLTextAreaElement).value,
    ).toBe("Indie rock guitarists");
    expect(
      (screen.getByLabelText("Key selling points") as HTMLTextAreaElement).value,
    ).toBe("Coil-split versatility");
    expect(
      (screen.getByLabelText("Target search trend") as HTMLInputElement).value,
    ).toBe("tiny desk concert");
    expect(
      (
        screen.getByLabelText(
          /Ground in Enterprise Knowledge Base/i,
        ) as HTMLInputElement
      ).checked,
    ).toBe(true);

    // Switching back to trend_scout in Quick form still remembers JP + Tokyo
    const scoutRadio = document.querySelector(
      'input[name="agent"][value="trend_scout"]',
    ) as HTMLInputElement;
    fireEvent.click(scoutRadio);
    expect((screen.getByLabelText("Country") as HTMLSelectElement).value).toBe(
      "JP",
    );
    expect(
      (screen.getByLabelText("Region or metro (optional)") as HTMLInputElement)
        .value,
    ).toBe("Tokyo");
  });

  it("fetches Brand Assist in Guided brief, renders product chips & 60-30-10 swatches, and applies them to shared form state", async () => {
    fetchBrandAssistMock.mockResolvedValue(SAMPLE_ASSIST);
    render(<Home />);
    await waitFor(() => expect(listSessionsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("tab", { name: "Guided brief" }));

    fireEvent.change(screen.getByLabelText("Brand name"), {
      target: { value: "PRS Guitars" },
    });
    fireEvent.change(screen.getByLabelText("Brand domain hint (optional)"), {
      target: { value: "prsguitars.com" },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Auto-fill with Brand Assist" }),
    );

    await waitFor(() => {
      expect(fetchBrandAssistMock).toHaveBeenCalledWith(
        "me",
        {
          brand: "PRS Guitars",
          target_product: "",
          domain_hint: "prsguitars.com",
        },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
    });

    // Renders product chips and 60-30-10 palette swatches
    expect(await screen.findByText("PRS Silver Sky")).toBeTruthy();
    expect(screen.getByTestId("palette-swatch-#1A1A1A")).toBeTruthy();
    expect(screen.getByTestId("palette-swatch-#F5F2EB")).toBeTruthy();
    expect(screen.getByTestId("palette-swatch-#C9A227")).toBeTruthy();

    // Apply product chip, audience & selling points, palette, and visual cue
    fireEvent.click(screen.getByRole("button", { name: "PRS Silver Sky" }));
    expect(
      (screen.getByLabelText("Target product") as HTMLInputElement).value,
    ).toBe("PRS Silver Sky");

    fireEvent.click(
      screen.getByRole("button", { name: "Apply audience & selling points" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Apply palette" }));
    fireEvent.click(screen.getByRole("button", { name: "+ bird fret inlays" }));

    // Switch to creative_agent on Step 2, then verify Step 3 has audience, selling points, palette, and cue!
    fireEvent.click(screen.getByRole("button", { name: "2. Trend & Market" }));
    fireEvent.click(
      document.querySelector(
        'input[name="agent"][value="creative_agent"]',
      ) as HTMLInputElement,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "3. Audience & Launch" }),
    );

    expect(
      (screen.getByLabelText("Target audience") as HTMLTextAreaElement).value,
    ).toBe(SAMPLE_ASSIST.target_audience);
    expect(
      (screen.getByLabelText("Key selling points") as HTMLTextAreaElement).value,
    ).toBe(SAMPLE_ASSIST.key_selling_points);
    expect(
      (screen.getByLabelText("Brand colors") as HTMLInputElement).value,
    ).toBe("60% #1A1A1A, 30% #F5F2EB, 10% #C9A227");
    expect(
      (screen.getByLabelText("Art direction") as HTMLTextAreaElement).value,
    ).toBe("bird fret inlays");
  });

  it("prevents implicit early form submission on Step 1/2 plain Enter while keeping #domain-hint Enter and Ctrl/Meta+Enter working", async () => {
    fetchBrandAssistMock.mockResolvedValue(SAMPLE_ASSIST);
    createSessionMock.mockResolvedValue({ id: "sess-guided-1" });

    render(<Home />);
    await waitFor(() => expect(listSessionsMock).toHaveBeenCalled());

    // Pre-fill a completely valid brief in Quick form first
    fireEvent.change(screen.getByLabelText("Brand name"), {
      target: { value: "PRS Guitars" },
    });
    fireEvent.change(screen.getByLabelText("Target product"), {
      target: { value: "SE CE24" },
    });
    fireEvent.change(screen.getByLabelText("Target audience"), {
      target: { value: "Indie rock guitarists" },
    });
    fireEvent.change(screen.getByLabelText("Key selling points"), {
      target: { value: "Bolt-on maple snap and 85/15 'S' coil-split" },
    });

    // Switch to Guided brief (Step 1)
    fireEvent.click(screen.getByRole("tab", { name: "Guided brief" }));

    // Plain Enter on #domain-hint prevents implicit form submit and triggers Brand Assist
    const domainInput = screen.getByLabelText("Brand domain hint (optional)");
    fireEvent.change(domainInput, { target: { value: "prsguitars.com" } });
    const domainEnterNotCancelled = fireEvent.keyDown(domainInput, {
      key: "Enter",
    });
    expect(domainEnterNotCancelled).toBe(false);
    await waitFor(() => expect(fetchBrandAssistMock).toHaveBeenCalledTimes(1));
    expect(createSessionMock).not.toHaveBeenCalled();

    // Navigate to Step 2 (trend_scout active): plain Enter on #trend-region does NOT submit
    fireEvent.click(screen.getByRole("button", { name: "2. Trend & Market" }));
    const regionInput = screen.getByLabelText("Region or metro (optional)");
    fireEvent.change(regionInput, { target: { value: "New York" } });
    const regionEnterNotCancelled = fireEvent.keyDown(regionInput, {
      key: "Enter",
    });
    expect(regionEnterNotCancelled).toBe(false);
    expect(createSessionMock).not.toHaveBeenCalled();

    // Switch to creative_agent on Step 2 and enter a trend so the creative brief is valid
    fireEvent.click(
      document.querySelector(
        'input[name="agent"][value="creative_agent"]',
      ) as HTMLInputElement,
    );
    const trendInput = screen.getByLabelText("Target search trend");
    fireEvent.change(trendInput, { target: { value: "tiny desk concert" } });

    // Plain Enter on #trend prevents implicit early submission
    const trendEnterNotCancelled = fireEvent.keyDown(trendInput, {
      key: "Enter",
    });
    expect(trendEnterNotCancelled).toBe(false);
    expect(createSessionMock).not.toHaveBeenCalled();

    // Ctrl+Enter on #trend bubbles to <form> and submits the valid brief
    fireEvent.keyDown(trendInput, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(1));

    // Meta+Enter also submits when ready
    await waitFor(() => expect(pushMock).toHaveBeenCalledTimes(1));
    fireEvent.keyDown(trendInput, { key: "Enter", metaKey: true });
    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(2));
  });

  it("extracts FastAPI 422 validation errors in fetchBrandAssist and filters invalid hex colors in formatPaletteForBrief", async () => {
    expect(
      formatPaletteForBrief(["#1a1a1a", "javascript:alert(1)", "#123", "#c9a227"]),
    ).toBe("70% #1A1A1A, 30% #C9A227");
    expect(formatPaletteForBrief(["not-a-color"])).toBe("");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: [
            {
              type: "value_error",
              loc: ["body", "domain_hint"],
              msg: "Value error, domain_hint must be a domain name, not an IP address",
            },
          ],
        }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      ),
    );
    try {
      const actualApi =
        await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      await expect(
        actualApi.fetchBrandAssist("me", {
          brand: "PRS",
          domain_hint: "127.0.0.1",
        }),
      ).rejects.toThrow("domain_hint must be a domain name, not an IP address");
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

describe("PipelineInspector live observability drawer", () => {
  it("is collapsed and lazy-mounted by default, and renders queries, sources, gate rounds, brand history, and residuals when opened", () => {
    const state: Record<string, unknown> = {
      initial_gs_queries: JSON.stringify({
        queries: [{ search_query: "tiny desk concert cultural moment" }],
      }),
      initial_campaign_queries: {
        queries: [{ search_query: "PRS SE CE24 review tone" }],
      },
      initial_enterprise_queries: {
        queries: [{ search_query: "PRS brand voice guidelines" }],
      },
      sources: {
        "src-1": {
          title: "NPR Music Tiny Desk",
          domain: "npr.org",
          url: "https://npr.org/tinydesk",
        },
        "src-2": {
          title: "Guitar World SE CE24",
          domain: "guitarworld.com",
          url: "https://guitarworld.com/prs",
        },
      },
      enterprise_sources: {
        "ent-1": {
          title: "gs://brand-kb/prs_style_guide_2026.pdf",
          source_type: "enterprise",
        },
      },
      combined_research_evaluation: {
        finding_type: "Opportunity",
        analysis_comment: "Strong acoustic-to-electric crossover angle.",
        follow_up_queries: [{ search_query: "indie session guitarist pedalboard" }],
      },
      brief_revision_rounds_used: 1,
      ad_copy_revision_rounds_used: 2,
      visual_concept_revision_rounds_used: 0,
      brand_history:
        "Prior PRS runs scored 0.86; avoid neon synthwave backdrops.",
      reference_product_description:
        "Faded Blue Smokeburst figured maple top with satin maple neck.",
      ad_copy_critique__issues: ["Copy #3 CTA exceeded 8 words"],
      enterprise_research_findings__retry_exhausted: true,
    };

    render(<PipelineInspector sessionState={state} appName="creative_agent" />);

    // Collapsed by default: summary counters render, inner sections are NOT mounted
    expect(screen.getByText("Pipeline inspector")).toBeTruthy();
    expect(screen.getByText("3 sources")).toBeTruthy();
    expect(screen.getByText("3 gate revisions")).toBeTruthy();
    expect(screen.getByText("2 warnings")).toBeTruthy();
    expect(
      screen.queryByText("tiny desk concert cultural moment"),
    ).toBeNull();

    // Open disclosure
    fireEvent.click(screen.getByText("Pipeline inspector"));

    // Queries
    expect(screen.getByText("tiny desk concert cultural moment")).toBeTruthy();
    expect(screen.getByText("PRS SE CE24 review tone")).toBeTruthy();
    expect(screen.getByText("PRS brand voice guidelines")).toBeTruthy();

    // Sources (with gs:// stripped from enterprise title)
    expect(screen.getByText("NPR Music Tiny Desk")).toBeTruthy();
    expect(screen.getByText("prs_style_guide_2026.pdf")).toBeTruthy();

    // Refinement evaluation
    expect(
      screen.getByText("Strong acoustic-to-electric crossover angle."),
    ).toBeTruthy();

    // Gate rounds
    expect(screen.getByText("1 revision")).toBeTruthy();
    expect(screen.getByText("2 revisions")).toBeTruthy();
    expect(screen.getByText("0 revisions")).toBeTruthy();

    // Brand history & reference asset descriptor
    expect(
      screen.getByText(/Prior PRS runs scored 0.86/i),
    ).toBeTruthy();
    expect(
      screen.getByText(/Faded Blue Smokeburst figured maple top/i),
    ).toBeTruthy();

    // Residuals & retry exhaustion
    expect(screen.getByText("Ad copy check")).toBeTruthy();
    expect(screen.getByText("Copy #3 CTA exceeded 8 words")).toBeTruthy();
    expect(screen.getByText("Enterprise knowledge base")).toBeTruthy();
    expect(screen.getByText("Retry exhausted")).toBeTruthy();
  });

  it("shows 'All quality gates clean' and regional scope when clean on trend_scout", () => {
    render(
      <PipelineInspector
        sessionState={{ trend_country_code: "GB", trend_region: "England" }}
        appName="trend_scout"
      />,
    );
    fireEvent.click(screen.getByText("Pipeline inspector"));
    expect(screen.getByText("All quality gates clean")).toBeTruthy();
    expect(
      screen.getByText("Country: GB · Region / metro: England"),
    ).toBeTruthy();
  });

  it("safely parses malformed JSON strings, nulls, primitives, and arrays in PipelineInspector helpers", () => {
    // extractSearchQueries
    expect(extractSearchQueries(null)).toEqual([]);
    expect(extractSearchQueries(undefined)).toEqual([]);
    expect(extractSearchQueries("")).toEqual([]);
    expect(extractSearchQueries(42)).toEqual([]);
    expect(extractSearchQueries("{malformed json}")).toEqual([
      "{malformed json}",
    ]);
    expect(extractSearchQueries("[broken")).toEqual(["[broken"]);
    expect(
      extractSearchQueries([
        "  direct query ",
        "",
        null,
        { query: " fallback query " },
        { search_query: "primary query" },
        { unrelated: 123 },
      ]),
    ).toEqual(["direct query", "fallback query", "primary query"]);

    // extractSourceItems
    expect(extractSourceItems(null)).toEqual([]);
    expect(extractSourceItems("{malformed}")).toEqual([]);
    expect(extractSourceItems(123)).toEqual([]);
    expect(
      extractSourceItems(
        ["gs://kb-bucket/brand_guide.pdf", null, { url: "https://prs.com" }],
        true,
      ),
    ).toEqual([
      { id: "item-1", label: "brand_guide.pdf" },
      { id: "item-3", label: "https://prs.com" },
    ]);

    // extractResiduals
    expect(
      extractResiduals({
        empty__issues: [],
        blank__issues: ["   ", null],
        false__retry_exhausted: false,
        malformed_json__issues: "{unclosed_issue",
        json_array__issues: JSON.stringify(["Gate warning A", ""]),
        search_step__retry_exhausted: true,
      }),
    ).toEqual([
      {
        key: "json_array__issues",
        stageLabel: "Json array",
        kind: "issues",
        items: ["Gate warning A"],
      },
      {
        key: "malformed_json__issues",
        stageLabel: "Malformed json",
        kind: "issues",
        items: ["{unclosed_issue"],
      },
      {
        key: "search_step__retry_exhausted",
        stageLabel: "Search step",
        kind: "retry_exhausted",
        items: ["Step 'search_step' exhausted retries and produced no output."],
      },
    ]);
  });
});
