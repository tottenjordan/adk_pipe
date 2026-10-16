// @vitest-environment jsdom
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Rating } from "@/lib/ratings";
import type { Proof, VisualConcept, AdCopy } from "@/lib/eval-matching";

const putRating = vi.fn();
const getRatings = vi.fn();
vi.mock("@/lib/api", () => ({
  putRating: (...a: unknown[]) => putRating(...a),
  getRatings: (...a: unknown[]) => getRatings(...a),
}));

import {
  CreativeRatings,
  RatingControl,
  SAVED_CLEAR_MS,
  useSessionRatings,
} from "@/app/results/[sessionId]/rating-control";
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
      fail_reasons: [],
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
  const proof = (adCopy?: Partial<AdCopy>, adCopyMatch: Proof["adCopyMatch"] = adCopy ? "id" : null): Proof => ({
    index: 0,
    concept: { concept_name: "The Jackpot Reveal", headline: "H", ad_copy_id: 3 } as VisualConcept,
    adCopy: adCopy as AdCopy | undefined,
    adCopyMatch,
  });

  it("renders a control per kind, seeded from the saved ratings", () => {
    render(
      <CreativeRatings
        proof={proof({ original_id: 3 })}
        appName="creative_agent"
        sessionId="s1"
        byKey={{ "copy:3": { creative_key: "copy:3", kind: "ad_copy", verdict: "pass", score: 5, note: null } }}
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
      <CreativeRatings proof={proof()} appName="creative_agent" sessionId="s1" byKey={{}} onChange={vi.fn()} />
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save ad copy rating" })).toBeNull());
    expect(screen.getByRole("button", { name: "Save visual rating" })).toBeInTheDocument();
    expect(screen.queryByText(/Can't rate this copy/)).toBeNull();
  });

  it("disables the ad copy rating with a reason when the copy was only paired by position", () => {
    render(
      <CreativeRatings
        proof={proof({ original_id: 3 }, "index")}
        appName="creative_agent"
        sessionId="s1"
        byKey={{ "copy:3": { creative_key: "copy:3", kind: "ad_copy", verdict: "pass", score: 5, note: null } }}
        onChange={vi.fn()}
      />
    );
    const copy = screen.getByTestId("rating-ad_copy");
    expect(copy).toHaveTextContent("Ad copy");
    expect(copy).toHaveTextContent(
      "Can't rate this copy: it couldn't be matched to this concept reliably."
    );
    expect(screen.queryByRole("button", { name: "Save ad copy rating" })).toBeNull();
    // another copy's saved rating is not shown against this concept
    expect(screen.queryByText(/5\/5/)).toBeNull();
    // the visual rating still works
    fireEvent.click(screen.getByRole("button", { name: "Pass" }));
    expect(screen.getByRole("button", { name: "Save visual rating" })).toBeEnabled();
  });
});

describe("useSessionRatings", () => {
  const r = (key: string, verdict: "pass" | "fail", updated_at = "2026-10-07T10:00:00+00:00"): Rating => ({
    creative_key: key,
    kind: key.startsWith("copy:") ? "ad_copy" : "visual",
    verdict,
    score: null,
    note: null,
    updated_at,
  });

  it("keeps a rating saved before the initial load returns", async () => {
    let resolve!: (list: Rating[]) => void;
    getRatings.mockReturnValue(new Promise<Rating[]>((res) => (resolve = res)));
    const { result } = renderHook(() => useSessionRatings("s1", true));
    act(() => result.current.setRating(KEY, r(KEY, "fail", "2026-10-12T00:00:00+00:00")));
    await act(async () => {
      resolve([r(KEY, "pass"), r("copy:3", "pass")]);
    });
    expect(result.current.byKey[KEY].verdict).toBe("fail");
    expect(result.current.byKey["copy:3"].verdict).toBe("pass");
  });

  it("removes the key when a save is reverted to no rating", async () => {
    getRatings.mockResolvedValue([]);
    const { result } = renderHook(() => useSessionRatings("s1", true));
    await waitFor(() => expect(getRatings).toHaveBeenCalled());
    act(() => result.current.setRating(KEY, r(KEY, "pass")));
    expect(result.current.byKey[KEY]).toBeDefined();
    act(() => result.current.setRating(KEY, undefined));
    expect(KEY in result.current.byKey).toBe(false);
  });

  it("starts empty for another session instead of carrying the last one's ratings", async () => {
    getRatings.mockResolvedValueOnce([r(KEY, "pass")]).mockReturnValueOnce(new Promise(() => {}));
    const { result, rerender } = renderHook(({ id }) => useSessionRatings(id, true), {
      initialProps: { id: "s1" },
    });
    await waitFor(() => expect(result.current.byKey[KEY]).toBeDefined());
    rerender({ id: "s2" });
    expect(result.current.byKey).toEqual({});
  });
});

describe("ProofGrid rated mark", () => {
  const proofs: Proof[] = [0, 1].map((index) => ({
    index,
    concept: { concept_name: `C${index}`, headline: `H${index}`, ad_copy_id: index } as VisualConcept,
    adCopyMatch: null,
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

describe("RatingControl draft sync", () => {
  const saved = (verdict: "pass" | "fail", note = ""): Rating => ({
    creative_key: KEY,
    kind: "visual",
    verdict,
    score: null,
    note: note || null,
  });
  const el = (s?: Rating) => (
    <RatingControl appName="creative_agent" sessionId="s1" creativeKey={KEY} kind="visual" title="Visual" saved={s} onChange={vi.fn()} />
  );

  it("follows a saved rating that arrives after mount while untouched", () => {
    const { rerender } = render(el(undefined));
    expect(screen.getByRole("button", { name: "Pass" })).toHaveAttribute("aria-pressed", "false");
    rerender(el(saved("pass", "loaded later")));
    expect(screen.getByRole("button", { name: "Pass" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("loaded later");
  });

  it("keeps the user's edits when the saved rating changes afterwards", () => {
    const { rerender } = render(el(undefined));
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    fireEvent.change(screen.getByLabelText("Note (optional)"), { target: { value: "mine" } });
    rerender(el(saved("pass", "server")));
    expect(screen.getByRole("button", { name: "Fail" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Note (optional)")).toHaveValue("mine");
  });
});

describe("RatingControl fail-reason chips", () => {
  const el = (kind: "visual" | "ad_copy" = "visual", saved?: Rating) => (
    <RatingControl
      appName="creative_agent"
      sessionId="s1"
      creativeKey={kind === "visual" ? KEY : "copy:3"}
      kind={kind}
      title={kind === "visual" ? "Visual" : "Ad copy"}
      saved={saved}
      onChange={vi.fn()}
    />
  );

  it("reveals the chips only once Fail is chosen", () => {
    render(el());
    expect(screen.queryByRole("group", { name: "Why did it fail? (optional)" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    const group = screen.getByRole("group", { name: "Why did it fail? (optional)" });
    expect(group).toBeInTheDocument();
    const chip = screen.getByRole("button", { name: "Product hard to see" });
    expect(chip).toHaveAttribute("aria-pressed", "false");
    // muted chips: the action colour is for Save only
    expect(chip.className).not.toMatch(/\bbg-primary\b/);
  });

  it("sends the selected reasons and toggles them off again", async () => {
    putRating.mockResolvedValue({});
    render(el());
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    fireEvent.click(screen.getByRole("button", { name: "Product hard to see" }));
    fireEvent.click(screen.getByRole("button", { name: "In-image text problems" }));
    fireEvent.click(screen.getByRole("button", { name: "Trend unclear" }));
    fireEvent.click(screen.getByRole("button", { name: "Trend unclear" }));
    expect(screen.getByRole("button", { name: "Product hard to see" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Trend unclear" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByRole("button", { name: "Save visual rating" }));
    expect(putRating).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ verdict: "fail", fail_reasons: ["product_not_visible", "text_problem"] })
    );
    await waitFor(() => expect(screen.getByText("Saved")).toBeInTheDocument());
  });

  it("clears the reasons when switching to Pass", () => {
    render(el());
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    fireEvent.click(screen.getByRole("button", { name: "Unwanted logo / trademark" }));
    fireEvent.click(screen.getByRole("button", { name: "Pass" }));
    expect(screen.queryByRole("group", { name: "Why did it fail? (optional)" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    expect(screen.getByRole("button", { name: "Unwanted logo / trademark" })).toHaveAttribute("aria-pressed", "false");
  });

  it("offers copy-relevant reasons for ad copy and restores saved ones", () => {
    render(
      el("ad_copy", {
        creative_key: "copy:3",
        kind: "ad_copy",
        verdict: "fail",
        score: null,
        note: null,
        fail_reasons: ["weak_cta", "not_a_reason"],
      })
    );
    expect(screen.getByRole("button", { name: "Weak call to action" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("button", { name: "Product hard to see" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Visual artifacts / quality" })).toBeNull();
    expect(screen.getByRole("button", { name: "Save ad copy rating" })).toBeDisabled();
  });
});

describe("RatingControl feedback", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("clears the Saved confirmation after about 2.5 s", async () => {
    vi.useFakeTimers();
    putRating.mockResolvedValue({});
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Pass" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save visual rating" }));
    });
    expect(screen.getByText("Saved")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(SAVED_CLEAR_MS - 100));
    expect(screen.getByText("Saved")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(100));
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("keeps an error message until the next edit", async () => {
    vi.useFakeTimers();
    putRating.mockRejectedValue(new Error("boom"));
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Pass" }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save visual rating" }));
    });
    act(() => vi.advanceTimersByTime(SAVED_CLEAR_MS * 2));
    expect(screen.getByText(/Couldn.t save your rating/)).toBeInTheDocument();
  });

  it("reserves the fail-reason space on a pass so switching verdict doesn't shift the layout", () => {
    render(<Harness />);
    const reasons = screen.getByTestId("fail-reasons-visual");
    // present but invisible and out of the tab order / accessibility tree
    expect(reasons).toHaveClass("opacity-0");
    expect(reasons).toHaveAttribute("aria-hidden", "true");
    expect(reasons).toHaveAttribute("inert");
    fireEvent.click(screen.getByRole("button", { name: "Fail" }));
    expect(screen.getByTestId("fail-reasons-visual")).toBe(reasons);
    expect(reasons).toHaveClass("opacity-100");
    expect(reasons).not.toHaveAttribute("inert");
    // a click fades (opacity only, token duration)
    expect(reasons.className).toMatch(/transition-opacity/);
    expect(reasons.className).toMatch(/duration-\(--dur-pop\)/);
  });
});
