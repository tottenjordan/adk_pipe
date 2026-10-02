import { describe, it, expect } from "vitest";
import {
  CONTINUE_MESSAGE,
  imagesNotRendered,
  sessionStoppedEarly,
  stoppedEarly,
} from "@/lib/run-completion";
import { deriveStages } from "@/lib/run-stages";
import type { PauseContext } from "@/lib/pause-detection";
import type { AgentEvent } from "@/lib/types";

const pause: PauseContext = {
  functionCallId: "fc-1",
  functionName: "review_visual_concepts",
  eventId: "ev-1",
};

const UP_TO_VISUALS = {
  combined_final_cited_report: "report",
  research_report_gcs_uri: "gs://b/r.pdf",
  ad_copy_critique: { ad_copies: [{ headline: "h" }] },
  final_visual_concepts: { visual_concepts: [{ concept_name: "c" }] },
};
const CREATIVE_DONE = {
  ...UP_TO_VISUALS,
  _images_generated: true,
  eval_report_gcs_uri: "gs://b/e.json",
};

describe("stoppedEarly", () => {
  it("creative_agent stopped after visual concepts → before Images", () => {
    expect(stoppedEarly("creative_agent", UP_TO_VISUALS, "completed", null)).toEqual({
      stage: "Images",
    });
  });

  it("creative_agent stopped after images → before Evaluation & save", () => {
    expect(
      stoppedEarly("creative_agent", { ...UP_TO_VISUALS, _images_generated: true }, "completed", null)
    ).toEqual({ stage: "Evaluation & save" });
  });

  it("interactive_creative stopped after visual concepts → before Review visuals", () => {
    expect(stoppedEarly("interactive_creative", UP_TO_VISUALS, "completed", null)).toEqual({
      stage: "Review visuals",
    });
  });

  it("interactive_creative with the checkpoint answered → before Images", () => {
    expect(
      stoppedEarly(
        "interactive_creative",
        UP_TO_VISUALS,
        "completed",
        null,
        new Set(["review_research", "review_ad_copies", "review_visual_concepts"])
      )
    ).toEqual({ stage: "Images" });
  });

  it("trend_scout stopped after research → before Write strategy", () => {
    expect(
      stoppedEarly("trend_scout", { raw_gtrends: "x", info_gtrends: "y" }, "completed", null)
    ).toEqual({ stage: "Write strategy" });
  });

  it("trend_scout with nothing written → before the first stage", () => {
    expect(stoppedEarly("trend_scout", {}, "completed", null)).toEqual({ stage: "Gather trends" });
  });

  it("is null while a review is pending", () => {
    expect(stoppedEarly("interactive_creative", UP_TO_VISUALS, "completed", pause)).toBeNull();
  });

  it("is null for non-completed statuses", () => {
    for (const s of ["running", "paused", "error", "stalled"] as const) {
      expect(stoppedEarly("creative_agent", UP_TO_VISUALS, s, null)).toBeNull();
    }
  });

  it("is null for truly complete runs", () => {
    expect(stoppedEarly("creative_agent", CREATIVE_DONE, "completed", null)).toBeNull();
    expect(stoppedEarly("interactive_creative", CREATIVE_DONE, "completed", null)).toBeNull();
    expect(
      stoppedEarly("trend_scout", { select_trends_markdown_gcs_uri: "gs://b/t.md" }, "completed", null)
    ).toBeNull();
  });

  it("is null for an unknown app", () => {
    expect(stoppedEarly("mystery_agent", {}, "completed", null)).toBeNull();
  });

  it("treats an empty final key as missing", () => {
    expect(
      stoppedEarly("creative_agent", { ...CREATIVE_DONE, eval_report_gcs_uri: "  " }, "completed", null)
    ).toEqual({ stage: "Evaluation & save" });
  });

  it("the spine shows the stopped stage as pending, not done", () => {
    const stages = deriveStages("interactive_creative", UP_TO_VISUALS, null, "completed");
    expect(stages.find((s) => s.label === "Review visuals")?.state).toBe("pending");
    expect(stages.find((s) => s.label === "Images")?.state).toBe("pending");
    expect(stages.find((s) => s.label === "Visual concepts")?.state).toBe("done");
  });
});

const call = (id: string, name: string) =>
  ({
    id: `ev-${id}`,
    author: "root_agent",
    longRunningToolIds: [id],
    content: { parts: [{ functionCall: { id, name } }] },
  }) as unknown as AgentEvent;
const answer = (id: string, name: string) =>
  ({
    id: `ev-r-${id}`,
    author: "user",
    content: { parts: [{ functionResponse: { id, name } }] },
  }) as unknown as AgentEvent;

describe("sessionStoppedEarly", () => {
  it("is null when the log ends at an unanswered checkpoint", () => {
    const events = [call("a", "review_visual_concepts")];
    expect(sessionStoppedEarly("interactive_creative", UP_TO_VISUALS, events)).toBeNull();
  });

  it("uses answered reviews from the log", () => {
    const events = [
      call("a", "review_visual_concepts"),
      answer("a", "review_visual_concepts"),
    ];
    expect(sessionStoppedEarly("interactive_creative", UP_TO_VISUALS, events)).toEqual({
      stage: "Images",
    });
  });

  it("flags a log that ends without a pause", () => {
    expect(sessionStoppedEarly("interactive_creative", UP_TO_VISUALS, [])).toEqual({
      stage: "Review visuals",
    });
  });

  it("is null for a finished run", () => {
    expect(sessionStoppedEarly("creative_agent", CREATIVE_DONE, [])).toBeNull();
  });
});

describe("imagesNotRendered", () => {
  it("is true with concepts but no rendered images", () => {
    expect(imagesNotRendered(UP_TO_VISUALS)).toBe(true);
  });
  it("is false once images rendered", () => {
    expect(imagesNotRendered(CREATIVE_DONE)).toBe(false);
  });
  it("is false with no concepts", () => {
    expect(imagesNotRendered({})).toBe(false);
  });
});

describe("CONTINUE_MESSAGE", () => {
  it("asks the agent to continue without repeating steps", () => {
    expect(CONTINUE_MESSAGE).toBe(
      "Continue the WORKFLOW from where it stopped. Do not repeat completed steps; call the next step now."
    );
  });
});
