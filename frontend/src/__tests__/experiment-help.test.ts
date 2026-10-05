import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import * as help from "@/lib/experiment-help";
import { CTR_MODE_OPTIONS, REWARD_MODE_OPTIONS, SCENARIO_OPTIONS, statusInfo } from "@/lib/experiments";

const SRC = path.resolve(__dirname, "..");
const UI_FILES = [
  "app/experiments/[experimentId]/page.tsx",
  "app/experiments/[experimentId]/experiment-charts.tsx",
  "app/experiments/[experimentId]/shift-timeline.tsx",
  "app/experiments/[experimentId]/shift-results.tsx",
  "app/experiments/[experimentId]/run-selector.tsx",
  "app/experiments/page.tsx",
  "app/results/[sessionId]/deploy-panel.tsx",
  "app/results/[sessionId]/reader-tuning.tsx",
  "components/scenario-overrides.tsx",
  "components/experiment-status.tsx",
];

const groups = help as unknown as Record<string, Record<string, string>>;

/** Every help string, labelled "GROUP.key". */
const allStrings = Object.entries(groups).flatMap(([group, entries]) =>
  Object.entries(entries).map(([key, text]) => [`${group}.${key}`, text] as const)
);

describe("experiment help copy", () => {
  it("every GROUP.key referenced in the UI exists", () => {
    const refs = new Set<string>();
    for (const f of UI_FILES) {
      const src = readFileSync(path.join(SRC, f), "utf8");
      for (const m of src.matchAll(/\b([A-Z][A-Z_]+_(?:HELP|CONFIRM))\.(\w+)/g)) refs.add(`${m[1]}.${m[2]}`);
    }
    expect(refs.size).toBeGreaterThan(15);
    for (const ref of refs) {
      const [group, key] = ref.split(".");
      expect(groups[group]?.[key], ref).toBeTruthy();
    }
  });

  it("covers every status and deploy option", () => {
    for (const s of ["deploying", "ready", "running_traffic", "stopping", "stopped", "failed", "expired"]) {
      expect(help.STATUS_HELP[s as keyof typeof help.STATUS_HELP], s).toBeTruthy();
      expect(statusInfo(s).label).not.toBe(s);
    }
    for (const o of SCENARIO_OPTIONS) expect(help.SCENARIO_HELP[o.value]).toBeTruthy();
    for (const o of CTR_MODE_OPTIONS) expect(help.CTR_MODE_HELP[o.value]).toBeTruthy();
    for (const o of REWARD_MODE_OPTIONS) expect(help.REWARD_HELP[o.value]).toBeTruthy();
  });

  it.each(allStrings)("%s is non-empty sentence-case plain text", (_name, text) => {
    expect(text.trim()).toBe(text);
    expect(text.length).toBeGreaterThan(5);
    expect(text).not.toMatch(/ {2}/);
    // Sentence case: starts with a capital, and isn't shouted.
    expect(text.charAt(0)).toMatch(/[A-Z]/);
    expect(text).not.toMatch(/\b[A-Z]{2,}[a-z]*\s+[A-Z]{2,}\b/);
  });

  it("every shift help and explain entry is used somewhere (contracts §10)", async () => {
    const explain = await import("@/lib/experiment-explain");
    const files = [
      ...UI_FILES,
      "app/experiments/[experimentId]/segment-grid.tsx",
      "app/experiments/[experimentId]/creative-detail.tsx",
    ];
    const src = files.map((f) => readFileSync(path.join(SRC, f), "utf8")).join("\n");
    const groups: [string, Record<string, string>][] = [
      ["SHIFT_HELP", help.SHIFT_HELP],
      ["SHIFT_EXPLAIN", explain.SHIFT_EXPLAIN],
    ];
    for (const [group, entries] of groups) {
      for (const key of Object.keys(entries)) {
        expect(src.includes(`${group}.${key}`), `${group}.${key}`).toBe(true);
      }
    }
  });

  it("states the Stop consequences explicitly", () => {
    expect(help.STOP_CONFIRM.title).toBe("Stop this experiment?");
    expect(help.STOP_CONFIRM.body).toMatch(/deletes the live endpoint/);
    expect(help.STOP_CONFIRM.body).toMatch(/Results and charts are kept/);
    expect(help.STOP_CONFIRM.confirm).toBe("Stop and delete endpoint");
  });

  it("says the first three charts plot strategies and the traffic chart plots creatives", () => {
    expect(help.CHART_HELP.avgReward).toMatch(/strategy/);
    expect(help.CHART_HELP.regret).toMatch(/strategy/);
    expect(help.CHART_HELP.optimalShare).toMatch(/strategy/);
    expect(help.CHART_HELP.trafficShare).toMatch(/one creative/);
  });
});
