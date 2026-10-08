// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from "vitest";
import {
  briefFromState,
  buildRunRows,
  deriveStatus,
  DUPLICATE_BRIEF_KEY,
  formatRunTime,
  formatTrend,
  inferAgent,
  RUN_ACTIVE_WINDOW_MS,
  stashDuplicateBrief,
  takeDuplicateBrief,
  toRunRow,
} from "@/lib/run-history";
import type { Session } from "@/lib/types";

const NOW = Date.UTC(2026, 9, 2, 12, 0, 0);
const MIN = 60_000;

function session(id: string, state: Record<string, unknown>, minutesAgo?: number): Session {
  return {
    id,
    appName: "trend_scout",
    userId: "me",
    state,
    events: [],
    ...(minutesAgo !== undefined && { lastUpdateTime: (NOW - minutesAgo * MIN) / 1000 }),
  };
}

describe("inferAgent", () => {
  it("prefers ui_app", () => {
    expect(inferAgent({ ui_app: "interactive_creative", agent_output_dir: "creative_output" })).toBe(
      "interactive_creative",
    );
  });
  it("falls back to agent_output_dir", () => {
    expect(inferAgent({ agent_output_dir: "trawler_output" })).toBe("trend_scout");
    expect(inferAgent({ agent_output_dir: "creative_output" })).toBe("creative_agent");
  });
  it("ignores an unknown ui_app and returns null when nothing matches", () => {
    expect(inferAgent({ ui_app: "bogus" })).toBeNull();
    expect(inferAgent({})).toBeNull();
  });
});

describe("deriveStatus", () => {
  const recent = NOW - 5 * MIN;
  const old = NOW - RUN_ACTIVE_WINDOW_MS - MIN;

  it("maps running and error markers", () => {
    expect(deriveStatus({ __run_status: "running" }, "creative_agent", old, NOW)).toBe("Running");
    expect(
      deriveStatus({ __run_status: "error", eval_report_gcs_uri: "gs://x" }, "creative_agent", old, NOW),
    ).toBe("Failed");
  });

  it("done + final key → Completed, per agent", () => {
    expect(
      deriveStatus({ __run_status: "done", select_trends_markdown_gcs_uri: "gs://t" }, "trend_scout", old, NOW),
    ).toBe("Completed");
    for (const app of ["creative_agent", "interactive_creative"] as const) {
      expect(deriveStatus({ __run_status: "done", eval_report_gcs_uri: "gs://e" }, app, old, NOW)).toBe(
        "Completed",
      );
    }
  });

  it("done without the agent's own final key is not Completed", () => {
    expect(
      deriveStatus({ __run_status: "done", eval_report_gcs_uri: "gs://e" }, "trend_scout", old, NOW),
    ).toBe("Incomplete");
  });

  it("done without output → Needs review only for runs that can pause", () => {
    expect(deriveStatus({ __run_status: "done" }, "interactive_creative", old, NOW)).toBe("Needs review");
    expect(
      deriveStatus({ __run_status: "done", interactive_trend_pick: true }, "trend_scout", old, NOW),
    ).toBe("Needs review");
    expect(deriveStatus({ __run_status: "done" }, "trend_scout", old, NOW)).toBe("Incomplete");
    expect(deriveStatus({ __run_status: "done" }, "creative_agent", old, NOW)).toBe("Incomplete");
    expect(deriveStatus({ __run_status: "done" }, null, old, NOW)).toBe("Incomplete");
  });

  it("treats an empty final key as missing", () => {
    expect(deriveStatus({ __run_status: "done", eval_report_gcs_uri: "" }, "creative_agent", old, NOW)).toBe(
      "Incomplete",
    );
  });

  describe("without a marker", () => {
    it("is Completed when the final key exists (pre-async-job runs)", () => {
      expect(deriveStatus({ eval_report_gcs_uri: "gs://e" }, "creative_agent", old, NOW)).toBe("Completed");
      expect(deriveStatus({ eval_report_gcs_uri: "gs://e" }, null, old, NOW)).toBe("Completed");
    });
    it("is Not started when the agent never seeded state", () => {
      expect(deriveStatus({ ui_app: "creative_agent" }, "creative_agent", recent, NOW)).toBe("Not started");
    });
    it("is Running when seeded and recently updated, else Incomplete", () => {
      const state = { agent_output_dir: "creative_output" };
      expect(deriveStatus(state, "creative_agent", recent, NOW)).toBe("Running");
      expect(deriveStatus(state, "creative_agent", old, NOW)).toBe("Incomplete");
      expect(deriveStatus(state, "creative_agent", null, NOW)).toBe("Incomplete");
    });
  });
});

describe("formatTrend", () => {
  it.each([
    ["Powerball", "Powerball"],
    [["a", "b"], "a, b"],
    [{ target_search_trends: ["a", "b"] }, "a, b"],
    ["{'target_search_trends': ['tswift engaged', \"don't stop\"]}", "tswift engaged, don't stop"],
    ["['x', 'y']", "x, y"],
    [undefined, ""],
    [null, ""],
  ])("%j → %j", (input, expected) => {
    expect(formatTrend(input)).toBe(expected);
  });
});

describe("toRunRow / buildRunRows", () => {
  const sessions = [
    session("old-trend", { ui_app: "trend_scout", agent_output_dir: "trawler_output", brand: "PRS", __run_status: "done", select_trends_markdown_gcs_uri: "gs://t" }, 3000),
    session("creative", { agent_output_dir: "creative_output", brand: " Google ", target_search_trends: "{'target_search_trends': ['Powerball']}", __run_status: "done", eval_report_gcs_uri: "gs://e" }, 10),
    session("review", { ui_app: "interactive_creative", agent_output_dir: "creative_output", brand: "Coors", __run_status: "done" }, 60),
    session("no-time", {}),
  ];

  it("sorts newest first, untimed sessions last", () => {
    expect(buildRunRows(sessions, NOW).map((r) => r.id)).toEqual(["creative", "review", "old-trend", "no-time"]);
  });

  it("builds row fields and links completed creative runs to results", () => {
    const row = toRunRow(sessions[1], NOW);
    expect(row).toEqual({
      id: "creative",
      app: "creative_agent",
      agentLabel: "Creative run",
      brand: "Google",
      trend: "Powerball",
      updatedAt: NOW - 10 * MIN,
      status: "Completed",
      href: "/results/creative?app=creative_agent&userId=me",
      brief: { agent: "creative_agent", brand: "Google", targetSearchTrend: "Powerball" },
    });
  });

  it("links everything else to the run page", () => {
    const [trend, review, unknown] = [toRunRow(sessions[0], NOW), toRunRow(sessions[2], NOW), toRunRow(sessions[3], NOW)];
    expect(trend.href).toBe("/run/old-trend?app=trend_scout&userId=me");
    expect(trend.agentLabel).toBe("Trend scout");
    expect(review.status).toBe("Needs review");
    expect(review.agentLabel).toBe("Creative run with reviews");
    expect(review.href).toBe("/run/review?app=interactive_creative&userId=me");
    expect(unknown.agentLabel).toBe("Run");
    expect(unknown.status).toBe("Not started");
    expect(unknown.updatedAt).toBeNull();
  });
});

describe("formatRunTime", () => {
  it.each([
    [null, ""],
    [NOW - 10_000, "Just now"],
    [NOW - 12 * MIN, "12 min ago"],
    [NOW - 3 * 60 * MIN, "3 h ago"],
    [NOW - 30 * 60 * MIN, "Yesterday"],
  ])("%s → %s", (ts, expected) => {
    expect(formatRunTime(ts, NOW)).toBe(expected);
  });
  it("shows a short date beyond two days", () => {
    expect(formatRunTime(Date.UTC(2026, 8, 20, 12), NOW)).toBe("Sep 20");
    expect(formatRunTime(Date.UTC(2025, 8, 20, 12), NOW)).toBe("Sep 20, 2025");
  });
});

describe("briefFromState", () => {
  it("recovers campaign, trend, reference image and visual-intent fields", () => {
    expect(
      briefFromState({
        ui_app: "interactive_creative",
        brand: "PRS",
        target_audience: "Jam band fans",
        target_product: "SE CE24",
        key_selling_points: "Tone",
        target_search_trends: { target_search_trends: ["Powerball", "Phish"] },
        reference_image_uri: "gs://b/prs.png",
        reference_image_role: "product",
        visual_intent: "film noir",
        brand_colors: "gold",
        visual_style_preference: "",
        visual_avoid: "crowds",
        visual_aspect_ratio: "9:16",
        eval_report_gcs_uri: "gs://ignored",
      }),
    ).toEqual({
      agent: "interactive_creative",
      brand: "PRS",
      targetAudience: "Jam band fans",
      targetProduct: "SE CE24",
      keySellingPoints: "Tone",
      targetSearchTrend: "Powerball, Phish",
      referenceImageUri: "gs://b/prs.png",
      referenceImageRole: "product",
      visualIntent: "film noir",
      brandColors: "gold",
      visualAvoid: "crowds",
      visualAspectRatio: "9:16",
    });
  });

  it("recovers the extra reference rows after row 1", () => {
    const brief = briefFromState({
      ui_app: "creative_agent",
      reference_image_uri: "gs://b/p.png",
      reference_image_role: "product",
      reference_images: [
        { uri: "gs://b/p.png", role: "product" },
        { uri: "gs://b/s.png", role: "style" },
      ],
    });
    expect(brief.referenceImageUri).toBe("gs://b/p.png");
    expect(brief.extraReferenceImages).toEqual([{ uri: "gs://b/s.png", role: "style" }]);
  });

  it("moves the first reference into row 1 when only reference_images was seeded", () => {
    const brief = briefFromState({
      ui_app: "creative_agent",
      reference_images: [
        { uri: "gs://b/1.png", role: "logo" },
        { uri: "gs://b/2.png", role: "style" },
        { uri: "https://x/3.jpg", role: "product" },
      ],
    });
    expect(brief.referenceImageUri).toBe("gs://b/1.png");
    expect(brief.referenceImageRole).toBe("logo");
    expect(brief.extraReferenceImages).toEqual([
      { uri: "gs://b/2.png", role: "style" },
      { uri: "https://x/3.jpg", role: "product" },
    ]);
  });

  it("carries the trend-pick opt-in and omits an unknown agent", () => {
    expect(briefFromState({ ui_app: "trend_scout", interactive_trend_pick: true, brand: "X" })).toEqual({
      agent: "trend_scout",
      interactiveTrendPick: true,
      brand: "X",
    });
    expect(briefFromState({ brand: "X" })).toEqual({ brand: "X" });
  });

  it("restores the learn-from-ratings opt-in only when it was exactly true", () => {
    expect(
      briefFromState({ ui_app: "creative_agent", learn_from_ratings: true, brand: "X" }),
    ).toEqual({ agent: "creative_agent", learnFromRatings: true, brand: "X" });
    expect(
      briefFromState({ ui_app: "creative_agent", learn_from_ratings: false, brand: "X" }),
    ).toEqual({ agent: "creative_agent", brand: "X" });
    expect(briefFromState({ learn_from_ratings: "true", brand: "X" })).toEqual({ brand: "X" });
  });
});

describe("sessionStorage helpers", () => {
  beforeEach(() => sessionStorage.clear());

  it("stashes and takes a brief exactly once", () => {
    stashDuplicateBrief({ brand: "PRS" });
    expect(sessionStorage.getItem(DUPLICATE_BRIEF_KEY)).not.toBeNull();
    expect(takeDuplicateBrief()).toEqual({ brand: "PRS" });
    expect(takeDuplicateBrief()).toBeNull();
  });

  it("ignores a corrupt stash", () => {
    sessionStorage.setItem(DUPLICATE_BRIEF_KEY, "{not json");
    expect(takeDuplicateBrief()).toBeNull();
  });
});
