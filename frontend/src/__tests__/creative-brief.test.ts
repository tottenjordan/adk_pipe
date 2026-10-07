import { describe, it, expect } from "vitest";
import { fitModeLabel, parseCreativeBrief } from "@/lib/creative-brief";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

describe("parseCreativeBrief", () => {
  it("maps a full brief object to camelCase", () => {
    const brief = parseCreativeBrief(SAMPLE_CREATIVE_BRIEF);
    expect(brief).not.toBeNull();
    expect(brief!.singleMindedProposition).toBe("The only jackpot worth chasing is your own tone.");
    expect(brief!.reasonsToBelieve[1]).toEqual({
      claim: "The Powerball jackpot passed $1.8B this week.",
      sourceId: "src-2",
    });
    expect(brief!.brand.toneOfVoice).toBe("warm, witty, never smug");
    expect(brief!.brand.doNot).toEqual(["mock other guitar brands"]);
    expect(brief!.trendBridge).toMatchObject({ fitScore: 3, fitMode: "cultural" });
    expect(brief!.trendBridge.motifs).toEqual(["lottery ball draw", "golden ticket"]);
    expect(brief!.angles.map((a) => a.angleId)).toEqual(["A1", "A2", "A3"]);
    expect(brief!.desiredResponse).toContain("SE CE24");
  });

  it("accepts the brief as a JSON string", () => {
    expect(parseCreativeBrief(JSON.stringify(SAMPLE_CREATIVE_BRIEF))).toEqual(
      parseCreativeBrief(SAMPLE_CREATIVE_BRIEF)
    );
  });

  it("defaults missing lists and nested objects", () => {
    const brief = parseCreativeBrief({ single_minded_proposition: "One idea." });
    expect(brief).toEqual({
      objective: "",
      audience: "",
      insight: "",
      singleMindedProposition: "One idea.",
      reasonsToBelieve: [],
      brand: { toneOfVoice: "", distinctiveAssets: [], doNot: [] },
      trendBridge: { fitScore: null, fitMode: null, bridge: "", motifs: [], risks: [] },
      mandatories: [],
      avoid: [],
      desiredResponse: "",
      angles: [],
    });
  });

  it("drops malformed list entries", () => {
    const brief = parseCreativeBrief({
      single_minded_proposition: "One idea.",
      reasons_to_believe: [{ claim: "" }, "loose", { claim: "Real", source_id: null }],
      mandatories: ["a", 3, " ", null],
      angles: [null, {}, { name: "Only name" }],
    });
    expect(brief!.reasonsToBelieve).toEqual([{ claim: "Real", sourceId: null }]);
    expect(brief!.mandatories).toEqual(["a"]);
    expect(brief!.angles).toEqual([{ angleId: "", name: "Only name", tension: "", route: "" }]);
  });

  it("normalises fit score and mode", () => {
    const bridge = (b: Record<string, unknown>) =>
      parseCreativeBrief({ single_minded_proposition: "x", trend_bridge: b })!.trendBridge;
    expect(bridge({ fit_score: "4", fit_mode: "Light touch" })).toMatchObject({
      fitScore: 4,
      fitMode: "light_touch",
    });
    expect(bridge({ fit_score: 9, fit_mode: "sideways" })).toMatchObject({
      fitScore: null,
      fitMode: null,
    });
    expect(bridge({ fit_score: 0 }).fitScore).toBeNull();
  });

  it("returns null for absent or unusable values", () => {
    for (const v of [undefined, null, "", "  ", "not json", "[1,2]", 42, [], {}, { objective: "x" }]) {
      expect(parseCreativeBrief(v)).toBeNull();
    }
  });
});

describe("fitModeLabel", () => {
  it("labels each mode", () => {
    expect(fitModeLabel("direct")).toBe("Direct fit");
    expect(fitModeLabel("cultural")).toBe("Cultural fit");
    expect(fitModeLabel("light_touch")).toBe("Light touch");
    expect(fitModeLabel(null)).toBe("");
  });
});
