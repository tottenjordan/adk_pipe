import { describe, expect, it } from "vitest";
import {
  buildBriefEdit,
  emptyAngle,
  nextAngleId,
  toBriefPayload,
  validateBriefDraft,
} from "@/lib/brief-edit";
import { parseCreativeBrief, type CreativeBrief } from "@/lib/creative-brief";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

const brief = (): CreativeBrief => structuredClone(parseCreativeBrief(SAMPLE_CREATIVE_BRIEF)!);

describe("toBriefPayload", () => {
  it("round-trips the parsed brief back to the backend's snake_case schema", () => {
    expect(toBriefPayload(brief())).toEqual(SAMPLE_CREATIVE_BRIEF);
  });

  it("trims text, drops blank list rows and blank reasons, nulls blank source ids", () => {
    const draft = brief();
    draft.singleMindedProposition = "  Tone is the jackpot.  ";
    draft.mandatories = ["SE CE24", "  ", ""];
    draft.reasonsToBelieve = [
      { claim: "  Real claim ", sourceId: " " },
      { claim: "   ", sourceId: "src-2" },
    ];
    const payload = toBriefPayload(draft);
    expect(payload.single_minded_proposition).toBe("Tone is the jackpot.");
    expect(payload.mandatories).toEqual(["SE CE24"]);
    expect(payload.reasons_to_believe).toEqual([{ claim: "Real claim", source_id: null }]);
  });
});

describe("validateBriefDraft", () => {
  it("accepts the sample brief", () => {
    expect(validateBriefDraft(brief())).toEqual({});
  });

  it("requires a proposition, 3-5 named angles and a fit score + mode", () => {
    const draft = brief();
    draft.singleMindedProposition = "  ";
    draft.angles = draft.angles.slice(0, 2);
    draft.angles[1].name = "";
    draft.trendBridge.fitScore = null;
    draft.trendBridge.fitMode = null;
    expect(validateBriefDraft(draft)).toEqual({
      single_minded_proposition: "Add the single-minded proposition.",
      angles: "Keep at least 3 angles.",
      "angles.1.name": "Give this angle a name.",
      "trend_bridge.fit_score": "Choose a fit score from 1 to 5.",
      "trend_bridge.fit_mode": "Choose a fit mode.",
    });
  });

  it("caps angles at five", () => {
    const draft = brief();
    while (draft.angles.length < 6) draft.angles.push({ ...emptyAngle(draft.angles), name: "x" });
    expect(validateBriefDraft(draft).angles).toBe("Use at most 5 angles.");
  });
});

describe("angle ids", () => {
  it("fills the lowest free A1-A5 id", () => {
    const angles = brief().angles; // A1, A2, A3
    expect(nextAngleId(angles)).toBe("A4");
    expect(nextAngleId(angles.filter((a) => a.angleId !== "A2"))).toBe("A2");
    expect(emptyAngle(angles)).toEqual({ angleId: "A4", name: "", tension: "", route: "" });
  });
});

describe("buildBriefEdit", () => {
  it("sends nothing for an untouched (or whitespace-only edited) brief", () => {
    const draft = brief();
    expect(buildBriefEdit(brief(), draft)).toBeNull();
    draft.insight = `${draft.insight}  `;
    draft.avoid = [...draft.avoid, " "];
    expect(buildBriefEdit(brief(), draft)).toBeNull();
  });

  it("sends the full brief object under the creative_brief field", () => {
    const draft = brief();
    draft.singleMindedProposition = "Your tone is the jackpot.";
    const edit = buildBriefEdit(brief(), draft);
    expect(edit).toEqual([
      {
        field: "creative_brief",
        value: { ...SAMPLE_CREATIVE_BRIEF, single_minded_proposition: "Your tone is the jackpot." },
      },
    ]);
  });
});
