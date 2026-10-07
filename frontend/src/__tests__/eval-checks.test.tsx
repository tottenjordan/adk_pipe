// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  buildProofs,
  failedGates,
  proofFailedChecks,
  scoreGates,
  type CreativeScore,
  type EvalReport,
  type VisualConcept,
} from "@/lib/eval-matching";
import { gateLabel } from "@/lib/eval-dimensions";
import { CheckFailedChip, ChecksList } from "@/app/results/[sessionId]/eval-checks";
import { ProofGrid } from "@/app/results/[sessionId]/proof-grid";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";

const verdicts = [
  { dimension: "copy_quality", score: 9, verdict: "pass" as const, rationale: "r" },
];

function score(over: Partial<CreativeScore> = {}): CreativeScore {
  return {
    overall_score: 0.9,
    passed: true,
    verdicts,
    strengths: [],
    improvements: [],
    ...over,
  };
}

const GATED = score({
  passed: false,
  gates_passed: false,
  gates: [
    { gate: "product_visible", passed: true, note: "Skates centre frame." },
    { gate: "text_correct", passed: false, note: "Headline misspelled." },
    { gate: "brand_cue_present", passed: false, note: "No crate.", advisory: true },
  ],
});

describe("gate helpers", () => {
  it("are empty for reports written before gates", () => {
    expect(scoreGates(score())).toEqual([]);
    expect(failedGates(undefined)).toEqual([]);
  });

  it("drop malformed gates and ignore advisory failures", () => {
    const s = score({
      gates: [
        ...GATED.gates!,
        { gate: 3, passed: false } as never,
        null as never,
      ],
    });
    expect(scoreGates(s)).toHaveLength(3);
    expect(failedGates(s).map((g) => g.gate)).toEqual(["text_correct"]);
  });

  it("collect a proof's blocking failures across both evals", () => {
    const concept = { concept_name: "Dust", headline: "H", ad_copy_id: 1 } as VisualConcept;
    const report = {
      ad_copy_evaluations: [
        {
          original_id: 1,
          headline: "H",
          tone_style: "t",
          score: score({ gates: [{ gate: "product_named", passed: false }] }),
        },
      ],
      visual_concept_evaluations: [{ ad_copy_id: 1, concept_name: "Dust", score: GATED }],
    } as unknown as EvalReport;
    const [p] = buildProofs([concept], [], report);
    expect(proofFailedChecks(p).map((g) => g.gate)).toEqual(["product_named", "text_correct"]);
  });

  it("labels gates", () => {
    expect(gateLabel("text_correct")).toBe("In-image text correct");
    expect(gateLabel("some_new_gate")).toBe("Some new gate");
    expect(gateLabel("gates_reported")).toBe("Checks reported");
  });
});

describe("ChecksList", () => {
  it("renders nothing without gates", () => {
    const { container } = render(<ChecksList score={score()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("spells out pass/fail per gate, tags advisory checks and shows notes", () => {
    render(<ChecksList score={GATED} note="Judged from the image prompt." />);
    expect(screen.getByText("Checks")).toBeInTheDocument();
    const failRow = screen.getByText("In-image text correct").closest("li")!;
    const failMark = within(failRow).getByText("fail");
    expect(failMark).toHaveClass("text-mark-fail");
    expect(within(failRow).getByText("Headline misspelled.")).toBeInTheDocument();
    const passRow = screen.getByText("Product visible").closest("li")!;
    expect(within(passRow).getByText("pass")).toHaveClass("text-mark-pass");
    const cueRow = screen.getByText("Brand cue present").closest("li")!;
    expect(within(cueRow).getByText("advisory")).toBeInTheDocument();
    expect(within(passRow).queryByText("advisory")).toBeNull();
    expect(screen.getByText("Judged from the image prompt.")).toBeInTheDocument();
  });
});

describe("CheckFailedChip", () => {
  it("renders only for a positive count", () => {
    const { container } = render(<CheckFailedChip count={0} />);
    expect(container).toBeEmptyDOMElement();
    render(<CheckFailedChip count={2} />);
    const chip = screen.getByText("Check failed");
    expect(chip).toHaveClass("text-mark-fail");
    expect(chip).toHaveAttribute("title", "2 checks failed");
  });
});

const concept = {
  concept_name: "Dust",
  headline: "Beep beep",
  ad_copy_id: 1,
  concept_summary: "Skates in dust",
} as VisualConcept;

function reportWith(visualScore: CreativeScore, imageJudged?: boolean): EvalReport {
  return {
    ad_copy_evaluations: [],
    visual_concept_evaluations: [
      { ad_copy_id: 1, concept_name: "Dust", score: visualScore, image_judged: imageJudged },
    ],
  } as unknown as EvalReport;
}

function renderGrid(report: EvalReport) {
  return render(
    <ProofGrid
      proofs={buildProofs([concept], [], report)}
      sort="pipeline"
      onSortChange={() => {}}
      imageUrlFor={() => null}
      onOpen={() => {}}
      itemRef={() => null}
    />
  );
}

describe("contact sheet", () => {
  it("marks a creative with a failed check", () => {
    renderGrid(reportWith(GATED));
    expect(screen.getByText("Check failed")).toBeInTheDocument();
  });

  it("shows no chip for passing gates, advisory-only failures or old reports", () => {
    const advisoryOnly = score({
      gates: [{ gate: "brand_cue_present", passed: false, advisory: true }],
    });
    for (const s of [advisoryOnly, score()]) {
      const { unmount } = renderGrid(reportWith(s));
      expect(screen.queryByText("Check failed")).toBeNull();
      unmount();
    }
  });
});

function renderDetail(report: EvalReport) {
  return render(
    <ProofDetail
      proofs={buildProofs([concept], [], report)}
      open
      index={0}
      onIndexChange={() => {}}
      onClose={() => {}}
      imageUrlFor={() => null}
      returnFocusTo={() => null}
    />
  );
}

describe("proof detail", () => {
  it("shows checks above the advisory quality scores", () => {
    renderDetail(reportWith(GATED, false));
    const checks = screen.getByText("Checks");
    const quality = screen.getByText("Quality (advisory)");
    expect(checks.compareDocumentPosition(quality) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      screen.getByText("Judged from the image prompt (the rendered image was not judged).")
    ).toBeInTheDocument();
  });

  it("renders old reports without checks as before", () => {
    renderDetail(reportWith(score()));
    expect(screen.queryByText("Checks")).toBeNull();
    expect(screen.queryByText("Quality (advisory)")).toBeNull();
    expect(screen.getByText("Copy quality")).toBeInTheDocument();
  });
});
