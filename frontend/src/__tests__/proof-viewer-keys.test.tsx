// @vitest-environment jsdom
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AdCopy, Proof, VisualConcept } from "@/lib/eval-matching";

const putRating = vi.fn();
vi.mock("@/lib/api", () => ({
  putRating: (...a: unknown[]) => putRating(...a),
  getRatings: () => Promise.resolve([]),
}));

import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";
import { CreativeRatings } from "@/app/results/[sessionId]/rating-control";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

const proofs: Proof[] = [0, 1, 2].map((index) => ({
  index,
  concept: {
    concept_name: `C${index}`,
    headline: `Headline ${index}`,
    ad_copy_id: index,
    concept_summary: `Summary ${index}`,
  } as VisualConcept,
  adCopy: { original_id: index, headline: `Copy ${index}` } as AdCopy,
  adCopyMatch: "id",
}));

function Viewer({
  start = 1,
  extraSlot,
}: {
  start?: number;
  extraSlot?: (p: Proof) => ReactNode;
}) {
  const [index, setIndex] = useState<number>(start);
  return (
    <ProofDetail
      proofs={proofs}
      open
      index={index}
      onIndexChange={setIndex}
      onClose={() => {}}
      imageUrlFor={() => null}
      returnFocusTo={() => null}
      ratingSlot={(p) => (
        <CreativeRatings proof={p} appName="creative_agent" sessionId="s1" byKey={{}} onChange={vi.fn()} />
      )}
      shareSlot={extraSlot}
    />
  );
}

const press = (key: string, init: KeyboardEventInit = {}, target: Element | null = null) =>
  fireEvent.keyDown(target ?? document.activeElement ?? document.body, { key, ...init });

// hidden: true so the checks also work while another dialog marks the viewer aria-hidden.
const group = (name: string) => screen.getByRole("group", { name, hidden: true });
const visualVerdict = () => group("Your verdict on the visual");
const copyVerdict = () => group("Your verdict on the ad copy");
const pressed = (g: HTMLElement, name: string) =>
  within(g).getByRole("button", { name, hidden: true }).getAttribute("aria-pressed") === "true";

beforeEach(() => {
  putRating.mockReset();
});

describe("proof viewer paging keys", () => {
  it("pages with ← and →, and the buttons advertise the keys", async () => {
    render(<Viewer />);
    await screen.findByRole("dialog");
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    press("ArrowRight");
    expect(screen.getByText("3 of 3")).toBeInTheDocument();
    press("ArrowLeft");
    press("ArrowLeft");
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
    const prev = screen.getByRole("button", { name: "Previous creative" });
    const next = screen.getByRole("button", { name: "Next creative" });
    expect(prev).toHaveAttribute("aria-keyshortcuts", "ArrowLeft");
    expect(next).toHaveAttribute("aria-keyshortcuts", "ArrowRight");
    expect(next).toHaveClass("size-8"); // icon (32 px), not icon-sm
  });

  it("ignores arrows with a modifier held or while typing", async () => {
    render(<Viewer />);
    await screen.findByRole("dialog");
    for (const mod of ["ctrlKey", "metaKey", "altKey", "shiftKey"]) press("ArrowRight", { [mod]: true });
    press("ArrowRight", {}, screen.getAllByLabelText("Note (optional)")[0]);
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
  });
});

describe("proof viewer rating keys", () => {
  it("rates the visual by default: P, F, 1–5, 0 and S", async () => {
    putRating.mockResolvedValue({});
    render(<Viewer />);
    await screen.findByRole("dialog");
    press("p");
    expect(pressed(visualVerdict(), "Pass")).toBe(true);
    expect(pressed(copyVerdict(), "Pass")).toBe(false);
    press("f");
    expect(pressed(visualVerdict(), "Fail")).toBe(true);
    // key actions never animate: the fail reasons appear without the fade
    const reasons = screen.getByTestId("fail-reasons-visual");
    expect(reasons).toHaveClass("opacity-100");
    expect(reasons.className).not.toMatch(/transition/);
    const score = group("Your score for the visual, 1 to 5 (optional)");
    press("4");
    expect(pressed(score, "4")).toBe(true);
    press("0");
    expect(pressed(score, "No score")).toBe(true);
    press("2");
    await act(async () => {
      press("s");
    });
    expect(putRating).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ creative_key: "visual:C1", verdict: "fail", score: 2 })
    );
  });

  it("C and V switch the target to the ad copy and back", async () => {
    render(<Viewer />);
    await screen.findByRole("dialog");
    press("c");
    expect(copyVerdict()).toContainElement(document.activeElement as HTMLElement);
    press("p");
    expect(pressed(copyVerdict(), "Pass")).toBe(true);
    expect(pressed(visualVerdict(), "Pass")).toBe(false);
    press("v");
    press("f");
    expect(pressed(visualVerdict(), "Fail")).toBe(true);
    expect(pressed(copyVerdict(), "Pass")).toBe(true);
  });

  it("does nothing while typing in the note", async () => {
    render(<Viewer />);
    await screen.findByRole("dialog");
    const note = screen.getAllByLabelText("Note (optional)")[1];
    note.focus();
    press("p", {}, note);
    press("3", {}, note);
    expect(pressed(visualVerdict(), "Pass")).toBe(false);
    expect(pressed(copyVerdict(), "Pass")).toBe(false);
  });

  it("does nothing with Ctrl, ⌘, Alt or Shift held, or on key repeat", async () => {
    render(<Viewer />);
    await screen.findByRole("dialog");
    for (const mod of ["ctrlKey", "metaKey", "altKey", "shiftKey", "repeat"]) press("p", { [mod]: true });
    expect(pressed(visualVerdict(), "Pass")).toBe(false);
  });

  it("does nothing while another dialog (e.g. Share) is open on top", async () => {
    render(
      <Viewer
        extraSlot={() => (
          <Dialog defaultOpen>
            <DialogContent>
              <DialogTitle>Share</DialogTitle>
              <button type="button">Inside share</button>
            </DialogContent>
          </Dialog>
        )}
      />
    );
    const inside = await screen.findByRole("button", { name: "Inside share" });
    press("ArrowRight", {}, inside);
    press("p", {}, inside);
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    expect(pressed(visualVerdict(), "Pass")).toBe(false);
  });

  it("puts aria-keyshortcuts on the rating controls and shows the key legend", async () => {
    render(<Viewer />);
    await screen.findByRole("dialog");
    expect(within(visualVerdict()).getByRole("button", { name: "Pass" })).toHaveAttribute("aria-keyshortcuts", "P");
    expect(within(visualVerdict()).getByRole("button", { name: "Fail" })).toHaveAttribute("aria-keyshortcuts", "F");
    expect(screen.getByRole("button", { name: "Save visual rating" })).toHaveAttribute("aria-keyshortcuts", "S");
    expect(screen.getByTestId("rating-keys")).toHaveTextContent(/P.*pass/);
  });
});
