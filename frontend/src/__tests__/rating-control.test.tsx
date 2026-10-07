// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Rating } from "@/lib/ratings";
import type { Proof, VisualConcept, AdCopy } from "@/lib/eval-matching";

const putRating = vi.fn();
const getRatings = vi.fn();
vi.mock("@/lib/api", () => ({
  putRating: (...a: unknown[]) => putRating(...a),
  getRatings: (...a: unknown[]) => getRatings(...a),
}));

import { CreativeRatings, RatingControl } from "@/app/results/[sessionId]/rating-control";
import { ProofGrid } from "@/app/results/[sessionId]/proof-grid";

const KEY = "visual:The Jackpot Reveal";

function Harness({ saved: initial }: { saved?: Rating }) {
  const onChange = vi.fn();
  return (
    <RatingControl
      appName="creative_agent"
      sessionId="s1"
      creativeKey={KEY}
      kind="visual"
      title="Visual"
      saved={initial}
      onChange={onChange}
    />
  );
}

beforeEach(() => {
  putRating.mockReset();
  getRatings.mockReset();
});

describe("RatingControl", () => {
  it("starts unrated with Save disabled until a verdict is chosen", () => {
    render(<Harness />);
    expect(screen.getByText("Not rated yet")).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save visual rating" });
    expect(save).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Pass" }));
    expect(screen.getByRole("button", { name: "Pass" })).toHaveAttribute("aria-pressed", "true");
    expect(save).toBeEnabled();
    // accessible groups and the note's label
    expect(screen.getByRole("group", { name: "Your verdict on the visual" })).toBeInTheDocument();
    expect(screen.getByLabelText("Note (optional)")).toBeInTheDocument();
  });

  it("shows the saved rating with the verdict spelled out", () => {
    render(<Harness saved={{ creative_key: KEY, kind: "visual", verdict: "fail", score: 2, note: "flat" }} />);
    const mark = screen.getByText("fail");
    expect(mark).toHaveClass("text-mark-fail");
    expect(screen.getByText(/2\/5/)).toBeInTheDocument();
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("flat");
    // nothing changed yet
    expect(screen.getByRole("button", { name: "Save visual rating" })).toBeDisabled();
  });

  it("saves optimistically, then applies the server's rating", async () => {
    const onChange = vi.fn();
    putRating.mockResolvedValue({ rating_id: "r1", creative_key: KEY, judge_passed: true });
    render(
      <RatingControl appName="creative_agent" sessionId="s1" creativeKey={KEY} kind="visual" title="Visual" onChange={onChange} />
    );
    fireEvent.click(screen.getByRole("button", { name: "Pass" }));
    fireEvent.click(screen.getByRole("button", { name: "4" }));
    fireEvent.change(screen.getByLabelText("Note (optional)"), { target: { value: " Strong hook " } });
    fireEvent.click(screen.getByRole("button", { name: "Save visual rating" }));
    // optimistic update first
    expect(onChange).toHaveBeenNthCalledWith(1, KEY, expect.objectContaining({ verdict: "pass", score: 4 }));
    expect(putRating).toHaveBeenCalledWith("s1", {
      app_name: "creative_agent",
      creative_key: KEY,
      kind: "visual",
      verdict: "pass",
      score: 4,
      note: "Strong hook",
    });
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(2));
    expect(screen.getByText("Saved")).toHaveClass("text-muted-foreground");
    expect(onChange).toHaveBeenLastCalledWith(KEY, expect.objectContaining({ rating_id: "r1", judge_passed: true }));
  });

  it("reverts to the previous rating and keeps the draft when the save fails", async () => {
    const onChange = vi.fn();
    const previous: Rating = { creative_key: KEY, kind: "visual", verdict: "pass", score: null, note: null };
    putRating.mockRejectedValue(new Error("502"));
    render(
      <RatingControl
        appName="creative_agent"
        sessionId="s1"
        creativeKey={KEY}
        kind="visual"
        title="Visual"
        saved={previous}
        onChange={onChange}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    fireEvent.click(screen.getByRole("button", { name: "Save visual rating" }));
    await screen.findByText("Couldn't save your rating. Try again.");
    expect(onChange).toHaveBeenNthCalledWith(1, KEY, expect.objectContaining({ verdict: "fail" }));
    expect(onChange).toHaveBeenLastCalledWith(KEY, previous);
    expect(screen.getByRole("button", { name: "Fail" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Save visual rating" })).toBeEnabled();
  });
});

describe("CreativeRatings", () => {
  const proof = (adCopy?: Partial<AdCopy>): Proof => ({
    index: 0,
    concept: { concept_name: "The Jackpot Reveal", headline: "H", ad_copy_id: 3 } as VisualConcept,
    adCopy: adCopy as AdCopy | undefined,
  });

  it("renders a control per kind, seeded from the saved ratings", () => {
    render(
      <CreativeRatings
        proof={proof({ original_id: 3 })}
        appName="creative_agent"
        sessionId="s1"
        byKey={{ "copy:3": { creative_key: "copy:3", kind: "ad_copy", verdict: "pass", score: 5, note: null } }}
        loaded
        onChange={vi.fn()}
      />
    );
    expect(screen.getByRole("heading", { name: "Your rating" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save ad copy rating" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save visual rating" })).toBeInTheDocument();
    expect(screen.getByText(/5\/5/)).toBeInTheDocument();
  });

  it("omits the ad copy control without a paired copy", async () => {
    render(
      <CreativeRatings proof={proof()} appName="creative_agent" sessionId="s1" byKey={{}} loaded onChange={vi.fn()} />
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save ad copy rating" })).toBeNull());
    expect(screen.getByRole("button", { name: "Save visual rating" })).toBeInTheDocument();
  });
});

describe("ProofGrid rated mark", () => {
  const proofs: Proof[] = [0, 1].map((index) => ({
    index,
    concept: { concept_name: `C${index}`, headline: `H${index}`, ad_copy_id: index } as VisualConcept,
  }));

  it("marks only rated proofs, in muted text and in the accessible name", () => {
    render(
      <ProofGrid
        proofs={proofs}
        sort="pipeline"
        onSortChange={vi.fn()}
        imageUrlFor={() => null}
        onOpen={vi.fn()}
        itemRef={() => null}
        isRated={(p) => p.index === 1}
      />
    );
    const marks = screen.getAllByText("Rated");
    expect(marks).toHaveLength(1);
    expect(marks[0]).not.toHaveClass("text-mark-pass");
    expect(screen.getByRole("button", { name: "Open creative 2: H1 (rated)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open creative 1: H0" })).toBeInTheDocument();
  });

  it("shows no marks without isRated", () => {
    render(
      <ProofGrid proofs={proofs} sort="pipeline" onSortChange={vi.fn()} imageUrlFor={() => null} onOpen={vi.fn()} itemRef={() => null} />
    );
    expect(screen.queryByText("Rated")).toBeNull();
  });
});
