// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { buildProofs, imageCheckFor, type VisualConcept } from "@/lib/eval-matching";
import { ImageCheckSection } from "@/app/results/[sessionId]/proof-detail";

const record = (qa: unknown, attempts = 1) => ({
  Jackpot: { gcs_uri: "gs://b/x.png", artifact_key: "x.png", attempts, qa },
});

describe("imageCheckFor", () => {
  it("is undefined without generated_images, the concept or a qa verdict", () => {
    expect(imageCheckFor(undefined, "Jackpot")).toBeUndefined();
    expect(imageCheckFor({}, "Jackpot")).toBeUndefined();
    expect(imageCheckFor(record(null), "Jackpot")).toBeUndefined();
    expect(imageCheckFor(record({ failures: [] }), "Jackpot")).toBeUndefined();
  });

  it("reads a pass on the first render", () => {
    expect(imageCheckFor(record({ passed: true, failures: [] }), "Jackpot")).toEqual({
      passed: true,
      issues: [],
      rerenders: 0,
    });
  });

  it("reads failures and the re-render count, dropping blank/non-string issues", () => {
    const check = imageCheckFor(
      record({ passed: false, failures: ["swoosh logo", "", 3, "gibberish text"] }, 2),
      "Jackpot"
    );
    expect(check).toEqual({
      passed: false,
      issues: ["swoosh logo", "gibberish text"],
      rerenders: 1,
    });
  });

  it("is attached to proofs by concept name", () => {
    const concept = { concept_name: "Jackpot", headline: "H" } as VisualConcept;
    const [p] = buildProofs([concept], [], null, record({ passed: true, failures: [] }, 2));
    expect(p.imageCheck).toEqual({ passed: true, issues: [], rerenders: 1 });
    const [bare] = buildProofs([concept], [], null);
    expect(bare.imageCheck).toBeUndefined();
  });
});

describe("ImageCheckSection", () => {
  it("renders nothing without a check", () => {
    const { container } = render(<ImageCheckSection check={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a pass mark with the re-render note", () => {
    render(<ImageCheckSection check={{ passed: true, issues: [], rerenders: 1 }} />);
    expect(screen.getByText("Image check")).toBeInTheDocument();
    const mark = screen.getByText("passed");
    expect(mark).toHaveClass("text-mark-pass");
    expect(screen.getByText("Re-rendered once.")).toBeInTheDocument();
  });

  it("shows a fail mark with the issues list", () => {
    render(
      <ImageCheckSection
        check={{ passed: false, issues: ["swoosh logo on the shirt"], rerenders: 0 }}
      />
    );
    expect(screen.getByText("issues")).toHaveClass("text-mark-fail");
    expect(screen.getByText("swoosh logo on the shirt")).toBeInTheDocument();
    expect(screen.queryByText(/Re-rendered/)).toBeNull();
  });

  it("pluralises more than one re-render", () => {
    render(<ImageCheckSection check={{ passed: false, issues: [], rerenders: 2 }} />);
    expect(screen.getByText("Re-rendered 2 times.")).toBeInTheDocument();
  });
});
