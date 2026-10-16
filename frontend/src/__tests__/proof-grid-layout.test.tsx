// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { buildProofs, type CreativeScore, type EvalReport, type VisualConcept } from "@/lib/eval-matching";
import { ProofGrid } from "@/app/results/[sessionId]/proof-grid";

// Contact-sheet alignment: when any card carries a flag chip ("Check failed",
// "Timely", ...), every card reserves the flag row, so the score marks line up
// across a row of cards. With no chips anywhere, no card spends the space.

const score = (over: Partial<CreativeScore> = {}): CreativeScore => ({
  overall_score: 0.9,
  passed: true,
  verdicts: [],
  strengths: [],
  improvements: [],
  ...over,
});

const FAILED = score({ passed: false, gates: [{ gate: "text_correct", passed: false }] });

const concepts = ["Dust", "Rain"].map(
  (name, i) => ({ concept_name: name, headline: `Headline ${name}`, ad_copy_id: i + 1, concept_summary: name }) as VisualConcept
);

function renderGrid(dustScore: CreativeScore) {
  const report = {
    ad_copy_evaluations: [],
    visual_concept_evaluations: [
      { ad_copy_id: 1, concept_name: "Dust", score: dustScore },
      { ad_copy_id: 2, concept_name: "Rain", score: score() },
    ],
  } as unknown as EvalReport;
  return render(
    <ProofGrid
      proofs={buildProofs(concepts, [], report)}
      sort="pipeline"
      onSortChange={() => {}}
      imageUrlFor={() => null}
      onOpen={() => {}}
      itemRef={() => null}
    />
  );
}

const flagRows = (card: HTMLElement) => card.querySelectorAll('[data-slot="proof-flags"]');

describe("contact sheet: flag row", () => {
  it("reserves the flag row on every card when one card has a chip", () => {
    renderGrid(FAILED);
    const dust = screen.getByRole("button", { name: /Open creative 1/ });
    const rain = screen.getByRole("button", { name: /Open creative 2/ });
    expect(flagRows(dust)).toHaveLength(1);
    expect(flagRows(rain)).toHaveLength(1);
    expect(flagRows(rain)[0]).toBeEmptyDOMElement();
    expect(flagRows(dust)[0]).toHaveTextContent("Check failed");
    // Same reserved height on both, whatever they contain.
    expect(flagRows(rain)[0].className).toBe(flagRows(dust)[0].className);
  });

  it("renders no flag row when no card has a chip", () => {
    renderGrid(score());
    for (const card of screen.getAllByRole("button", { name: /Open creative/ })) {
      expect(flagRows(card)).toHaveLength(0);
    }
  });

  it("gives the card buttons press feedback with a reduced-motion fallback", () => {
    renderGrid(score());
    const card = screen.getByRole("button", { name: /Open creative 1/ });
    expect(card.className).toMatch(/\bactive:scale-\[0\.99\]/);
    expect(card.className).toMatch(/\bmotion-reduce:active:scale-100\b/);
  });
});
