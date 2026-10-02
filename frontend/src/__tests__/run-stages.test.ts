import { describe, it, expect } from "vitest";
import {
  answeredReviewNames,
  currentStage,
  deriveStages,
  isPopulated,
  type Stage,
} from "@/lib/run-stages";
import type { PauseContext } from "@/lib/pause-detection";

const pause = (functionName: string): PauseContext => ({
  functionCallId: `fc-${functionName}`,
  functionName,
  eventId: `ev-${functionName}`,
});

/** Compact `label:state` view so expectations read like the spine. */
const view = (stages: Stage[]) => stages.map((s) => `${s.label}:${s.state}`);

const CREATIVE_DONE = {
  combined_final_cited_report: "report",
  research_report_gcs_uri: "gs://b/r.pdf",
  ad_copy_critique: { ad_copies: [{ headline: "h" }] },
  final_visual_concepts: { visual_concepts: [{ concept_name: "c" }] },
  _images_generated: "4 images",
  eval_report_gcs_uri: "gs://b/e.json",
};

describe("isPopulated", () => {
  it("rejects empty values and empty shells", () => {
    expect(isPopulated(undefined)).toBe(false);
    expect(isPopulated(null)).toBe(false);
    expect(isPopulated("  ")).toBe(false);
    expect(isPopulated([])).toBe(false);
    expect(isPopulated({ target_search_trends: [] })).toBe(false);
    expect(isPopulated(false)).toBe(false);
  });

  it("accepts real output", () => {
    expect(isPopulated("x")).toBe(true);
    expect(isPopulated({ target_search_trends: ["Powerball"] })).toBe(true);
    expect(isPopulated(["a"])).toBe(true);
    expect(isPopulated(0)).toBe(true);
    expect(isPopulated(true)).toBe(true);
  });
});

describe("deriveStages — creative_agent", () => {
  it("starts with research active and the rest pending", () => {
    expect(view(deriveStages("creative_agent", {}, null, "running"))).toEqual([
      "Research:active",
      "Research report:pending",
      "Ad copy:pending",
      "Visual concepts:pending",
      "Images:pending",
      "Evaluation & save:pending",
    ]);
  });

  it("marks populated stages done and the next one active", () => {
    const state = {
      combined_final_cited_report: "r",
      research_report_gcs_uri: "gs://b/r.pdf",
      ad_copy_critique: { ad_copies: [{ headline: "h" }] },
    };
    expect(view(deriveStages("creative_agent", state, null, "running"))).toEqual([
      "Research:done",
      "Research report:done",
      "Ad copy:done",
      "Visual concepts:active",
      "Images:pending",
      "Evaluation & save:pending",
    ]);
  });

  it("treats a stage as done when a later stage is done", () => {
    const state = { ad_copy_critique: { ad_copies: [{ headline: "h" }] } };
    const stages = deriveStages("creative_agent", state, null, "running");
    expect(stages.slice(0, 3).every((s) => s.state === "done")).toBe(true);
  });

  it("marks everything done on a completed full run", () => {
    const stages = deriveStages("creative_agent", CREATIVE_DONE, null, "completed");
    expect(stages.every((s) => s.state === "done")).toBe(true);
  });

  it("does not fake completion for stages with no data", () => {
    const state = { combined_final_cited_report: "r" };
    expect(view(deriveStages("creative_agent", state, null, "completed"))).toEqual([
      "Research:done",
      "Research report:pending",
      "Ad copy:pending",
      "Visual concepts:pending",
      "Images:pending",
      "Evaluation & save:pending",
    ]);
  });

  it("keeps the stopped stage active on error", () => {
    const state = { combined_final_cited_report: "r" };
    const stages = deriveStages("creative_agent", state, null, "error");
    expect(stages[1]).toMatchObject({ id: "research_report", state: "active" });
  });

  it("keeps the active stage while stalled", () => {
    expect(deriveStages("creative_agent", {}, null, "stalled")[0].state).toBe("active");
  });

  it("marks images degraded when image retries were exhausted", () => {
    const state = {
      ...CREATIVE_DONE,
      _images_generated: undefined,
      _images_generated__retry_exhausted: true,
    };
    const stages = deriveStages("creative_agent", state, null, "completed");
    expect(stages.find((s) => s.id === "images")?.state).toBe("degraded");
    expect(stages.find((s) => s.id === "eval_save")?.state).toBe("done");
  });

  it("does not make the stage after a degraded one look done", () => {
    const state = {
      ...CREATIVE_DONE,
      _images_generated: undefined,
      eval_report_gcs_uri: undefined,
      _images_generated__retry_exhausted: true,
    };
    const stages = deriveStages("creative_agent", state, null, "running");
    expect(stages.find((s) => s.id === "images")?.state).toBe("degraded");
    expect(stages.find((s) => s.id === "eval_save")?.state).toBe("active");
  });

  it("marks research degraded for research-producer markers", () => {
    const state = {
      combined_final_cited_report: "r",
      gs_web_search_insights__retry_exhausted: true,
    };
    const stages = deriveStages("creative_agent", state, null, "running");
    expect(stages[0].state).toBe("degraded");
    expect(stages[1].state).toBe("active");
  });

  it("ignores falsy markers", () => {
    const state = { campaign_web_search_insights__retry_exhausted: false };
    expect(deriveStages("creative_agent", state, null, "running")[0].state).toBe("active");
  });
});

describe("deriveStages — interactive_creative", () => {
  const base = {
    combined_final_cited_report: "r",
    research_report_gcs_uri: "gs://b/r.pdf",
  };

  it("inserts the three review stages", () => {
    const labels = deriveStages("interactive_creative", {}, null, "running").map((s) => s.label);
    expect(labels).toEqual([
      "Research",
      "Research report",
      "Review research",
      "Ad copy",
      "Review ad copy",
      "Visual concepts",
      "Review visuals",
      "Images",
      "Evaluation & save",
    ]);
  });

  it("marks the paused review stage as needs review", () => {
    const stages = deriveStages("interactive_creative", base, pause("review_research"), "paused");
    expect(view(stages).slice(0, 4)).toEqual([
      "Research:done",
      "Research report:done",
      "Review research:needs_review",
      "Ad copy:pending",
    ]);
    expect(stages.some((s) => s.state === "active")).toBe(false);
  });

  it("marks review ad copy as needs review at checkpoint 2", () => {
    const state = { ...base, ad_copy_critique: { ad_copies: [{ headline: "h" }] } };
    const stages = deriveStages("interactive_creative", state, pause("review_ad_copies"), "paused");
    expect(stages.find((s) => s.id === "review_research")?.state).toBe("done");
    expect(stages.find((s) => s.id === "review_ad_copy")?.state).toBe("needs_review");
    expect(stages.find((s) => s.id === "visual_concepts")?.state).toBe("pending");
  });

  it("marks review visuals as needs review at checkpoint 3", () => {
    const state = {
      ...base,
      ad_copy_critique: { ad_copies: [{ headline: "h" }] },
      final_visual_concepts: { visual_concepts: [{ concept_name: "c" }] },
    };
    const stages = deriveStages(
      "interactive_creative",
      state,
      pause("review_visual_concepts"),
      "paused"
    );
    expect(stages.find((s) => s.id === "review_visuals")?.state).toBe("needs_review");
  });

  it("before the checkpoint is reached, the review stage is active", () => {
    const stages = deriveStages("interactive_creative", base, null, "running");
    expect(stages.find((s) => s.id === "review_research")?.state).toBe("active");
  });

  it("after a review is answered, the next stage becomes active", () => {
    const stages = deriveStages(
      "interactive_creative",
      base,
      null,
      "running",
      new Set(["review_research"])
    );
    expect(stages.find((s) => s.id === "review_research")?.state).toBe("done");
    expect(stages.find((s) => s.id === "ad_copy")?.state).toBe("active");
  });

  it("a re-asked review (revision requested) is needs review again", () => {
    const stages = deriveStages(
      "interactive_creative",
      base,
      pause("review_research"),
      "paused",
      new Set(["review_research"])
    );
    expect(stages.find((s) => s.id === "review_research")?.state).toBe("needs_review");
  });

  it("the live pause wins even when later stages already have data", () => {
    const stages = deriveStages(
      "interactive_creative",
      CREATIVE_DONE,
      pause("review_ad_copies"),
      "paused"
    );
    expect(stages.find((s) => s.id === "review_ad_copy")?.state).toBe("needs_review");
  });

  it("marks reviews done once later stages are done", () => {
    const stages = deriveStages("interactive_creative", CREATIVE_DONE, null, "completed");
    expect(stages.every((s) => s.state === "done")).toBe(true);
  });
});

describe("deriveStages — trend_scout", () => {
  it("has no pick stage unless interactive trend pick is on", () => {
    const labels = deriveStages("trend_scout", {}, null, "running").map((s) => s.label);
    expect(labels).toEqual(["Gather trends", "Research", "Write strategy", "Save"]);
  });

  it("ignores the empty target_search_trends seed", () => {
    const state = {
      raw_gtrends: ["a", "b"],
      target_search_trends: { target_search_trends: [] },
      interactive_trend_pick: true,
    };
    expect(view(deriveStages("trend_scout", state, null, "running"))).toEqual([
      "Gather trends:done",
      "Pick trends:active",
      "Research:pending",
      "Write strategy:pending",
      "Save:pending",
    ]);
  });

  it("pauses on pick trends", () => {
    const state = { raw_gtrends: ["a"], interactive_trend_pick: true };
    const stages = deriveStages("trend_scout", state, pause("review_trends"), "paused");
    expect(stages[1]).toMatchObject({ id: "pick_trends", state: "needs_review" });
  });

  it("marks pick trends done once trends are picked or later stages finish", () => {
    const picked = {
      raw_gtrends: ["a"],
      interactive_trend_pick: true,
      target_search_trends: { target_search_trends: ["a"] },
    };
    expect(deriveStages("trend_scout", picked, null, "running")[1].state).toBe("done");
    expect(deriveStages("trend_scout", picked, null, "running")[2].state).toBe("active");

    const later = { raw_gtrends: ["a"], interactive_trend_pick: true, info_gtrends: "x" };
    expect(deriveStages("trend_scout", later, null, "running")[1].state).toBe("done");
  });

  it("completes when the markdown is saved", () => {
    const state = {
      raw_gtrends: ["a"],
      info_gtrends: "i",
      selected_gtrends: "s",
      select_trends_markdown_gcs_uri: "gs://b/t.md",
    };
    expect(deriveStages("trend_scout", state, null, "completed").every((s) => s.state === "done")).toBe(true);
  });

  it("marks research degraded for info_gtrends exhaustion", () => {
    const state = { raw_gtrends: ["a"], info_gtrends__retry_exhausted: true };
    const stages = deriveStages("trend_scout", state, null, "running");
    expect(stages.find((s) => s.id === "research")?.state).toBe("degraded");
    expect(stages.find((s) => s.id === "write_strategy")?.state).toBe("active");
  });
});

describe("deriveStages — unknown app", () => {
  it("falls back to one generic stage", () => {
    expect(view(deriveStages("mystery", {}, null, "running"))).toEqual(["Run:active"]);
    expect(view(deriveStages("mystery", {}, null, "completed"))).toEqual(["Run:done"]);
  });
});

describe("currentStage", () => {
  it("prefers the review, then the active stage, then the last finished one", () => {
    const s = (id: string, state: Stage["state"]): Stage => ({ id, label: id, state });
    expect(currentStage([s("a", "done"), s("b", "needs_review")])?.id).toBe("b");
    expect(currentStage([s("a", "done"), s("b", "active"), s("c", "pending")])?.id).toBe("b");
    expect(currentStage([s("a", "done"), s("b", "degraded"), s("c", "pending")])?.id).toBe("b");
    expect(currentStage([s("a", "pending")])).toBeNull();
  });
});

describe("answeredReviewNames", () => {
  it("collects review function responses only", () => {
    const events = [
      { content: { parts: [{ functionResponse: { name: "review_research" } }] } },
      { content: { parts: [{ functionResponse: { name: "combined_research_pipeline" } }] } },
      { content: { parts: [{}] } },
      {},
    ];
    expect([...answeredReviewNames(events)]).toEqual(["review_research"]);
  });
});
