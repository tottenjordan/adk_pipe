// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { buildProofs, castingFor, type VisualConcept } from "@/lib/eval-matching";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";

const concept = {
  concept_name: "Hero",
  headline: "Beep",
  ad_copy_id: 1,
  casts_person_reference: true,
  person_casting_reason: "One hero, face clearly visible.",
} as VisualConcept;

describe("castingFor", () => {
  it("is a cast when the render record says so", () => {
    expect(castingFor(concept, { Hero: { cast: true } }, undefined)).toEqual({
      cast: true,
      reason: "One hero, face clearly visible.",
    });
  });

  it("reports a rejected person photo", () => {
    expect(castingFor(concept, { Hero: { cast: false } }, { Hero: "image_safety" })).toEqual({
      cast: false,
      reason: "One hero, face clearly visible.",
      rejected: "image_safety",
    });
  });

  it("is absent for uncast concepts and runs without a person", () => {
    expect(castingFor(concept, { Hero: { cast: false } }, undefined)).toBeUndefined();
    expect(castingFor(concept, { Hero: {} }, undefined)).toBeUndefined();
    expect(castingFor(concept, undefined, undefined)).toBeUndefined();
    expect(
      castingFor({ ...concept, casts_person_reference: false }, undefined, { Other: "x" }),
    ).toBeUndefined();
  });
});

function renderDetail(generated: unknown, rejected?: unknown) {
  return render(
    <ProofDetail
      proofs={buildProofs([concept], [], null, generated, rejected)}
      open
      index={0}
      onIndexChange={() => {}}
      onClose={() => {}}
      imageUrlFor={() => null}
      returnFocusTo={() => null}
    />,
  );
}

describe("proof detail casting", () => {
  it("shows the cast reason for a cast concept", () => {
    renderDetail({ Hero: { cast: true } });
    expect(screen.getByText("Cast")).toBeInTheDocument();
    expect(screen.getByText("Yes — One hero, face clearly visible.")).toBeInTheDocument();
  });

  it("notes a person photo rejected by the safety filter", () => {
    renderDetail({ Hero: { cast: false } }, { Hero: "image_safety" });
    expect(
      screen.getByText(
        "Person photo rejected by the safety filter; this image was made without the person.",
      ),
    ).toBeInTheDocument();
  });

  it("notes an unavailable person photo", () => {
    renderDetail({ Hero: { cast: false } }, { Hero: "photo_unavailable" });
    expect(
      screen.getByText("Person photo unavailable; this image was made without the person."),
    ).toBeInTheDocument();
  });

  it("shows nothing for an uncast concept", () => {
    renderDetail({ Hero: { cast: false } });
    expect(screen.queryByText("Cast")).toBeNull();
  });
});
