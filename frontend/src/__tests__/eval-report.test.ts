import { describe, it, expect, vi } from "vitest";
import { fetchEvalReport, parseEvalReport } from "@/lib/eval-report";
import { jsonResponse } from "./helpers";

// The creative eval report is the LAST artifact the pipeline writes to GCS, so the
// results page can load (and fetch it) a few seconds before it exists — a 404 race.
// fetchEvalReport retries on 404 (the file is still being written) and reports a
// distinct "pending" outcome instead of silently swallowing the miss.

const noSleep = () => Promise.resolve();

describe("fetchEvalReport", () => {
  it("returns the parsed report on a 200", async () => {
    const report = { summary: { overall_pass_rate: 0.8 } };
    const fetchImpl = vi.fn(async () => jsonResponse(report));

    const result = await fetchEvalReport("/api/gcs?x=1", { fetchImpl, sleep: noSleep });

    expect(result).toEqual({ status: "found", report });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("retries on 404 and succeeds once the report appears", async () => {
    const report = { summary: {} };
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(null, 404))
      .mockResolvedValueOnce(jsonResponse(null, 404))
      .mockResolvedValueOnce(jsonResponse(report, 200));

    const result = await fetchEvalReport("/api/gcs?x=1", {
      retries: 4,
      fetchImpl,
      sleep: noSleep,
    });

    expect(result).toEqual({ status: "found", report });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("returns 'pending' after exhausting retries on persistent 404", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(null, 404));

    const result = await fetchEvalReport("/api/gcs?x=1", {
      retries: 2,
      fetchImpl,
      sleep: noSleep,
    });

    expect(result).toEqual({ status: "pending" });
    // initial attempt + 2 retries
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("does not retry on a non-404 error and reports it", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(null, 500));

    const result = await fetchEvalReport("/api/gcs?x=1", {
      retries: 3,
      fetchImpl,
      sleep: noSleep,
    });

    expect(result.status).toBe("error");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("treats a thrown network error as retryable, then pending", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error("network down");
    });

    const result = await fetchEvalReport("/api/gcs?x=1", {
      retries: 1,
      fetchImpl,
      sleep: noSleep,
    });

    expect(result.status).toBe("pending");
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("makes exactly one attempt when retries is 0", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse(null, 404));

    const result = await fetchEvalReport("/api/gcs?x=1", {
      retries: 0,
      fetchImpl,
      sleep: noSleep,
    });

    expect(result).toEqual({ status: "pending" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe("parseEvalReport", () => {
  const score = (over: Record<string, unknown> = {}) => ({
    overall_score: 0.8,
    passed: true,
    verdicts: [],
    strengths: ["Clear"],
    improvements: [],
    ...over,
  });
  const summary = {
    total_ad_copies: 1,
    ad_copies_passed: 1,
    avg_ad_copy_score: 0.8,
    total_visual_concepts: 1,
    visual_concepts_passed: 0,
    avg_visual_score: 0.5,
    overall_pass_rate: 0.5,
    weakest_dimensions: ["clarity"],
  };
  const good = {
    brand: "Acme",
    target_product: "Rocket skates",
    target_search_trend: "Marathon",
    ad_copy_evaluations: [{ original_id: 1, headline: "Go", tone_style: "Playful", score: score() }],
    visual_concept_evaluations: [{ ad_copy_id: 1, concept_name: "Dust", score: score({ passed: false }) }],
    warnings: ["Research gap"],
    passing_threshold: 0.7,
    summary,
  };

  it("passes a well-formed report through unchanged", () => {
    expect(parseEvalReport(good)).toEqual(good);
  });

  it.each([null, undefined, 42, "report", true, [good]])("returns null for a non-object (%s)", (raw) => {
    expect(parseEvalReport(raw)).toBeNull();
  });

  it("returns null without a summary object", () => {
    expect(parseEvalReport({ ...good, summary: undefined })).toBeNull();
    expect(parseEvalReport({ ...good, summary: "n/a" })).toBeNull();
  });

  it("defaults missing or non-array evaluation lists to []", () => {
    const report = parseEvalReport({ summary, visual_concept_evaluations: "oops" });
    expect(report?.ad_copy_evaluations).toEqual([]);
    expect(report?.visual_concept_evaluations).toEqual([]);
    expect(report?.brand).toBe("");
  });

  it("drops entries without a usable score", () => {
    const report = parseEvalReport({
      ...good,
      ad_copy_evaluations: [
        { original_id: 1, headline: "No score" },
        { original_id: 2, headline: "Bad score", score: "0.9" },
        { original_id: 3, headline: "No number", score: { passed: true } },
        null,
        { original_id: 4, headline: "Kept", tone_style: "Bold", score: score() },
      ],
      visual_concept_evaluations: [{ concept_name: "Lost" }],
    });
    expect(report?.ad_copy_evaluations.map((e) => e.original_id)).toEqual([4]);
    expect(report?.visual_concept_evaluations).toEqual([]);
  });

  it("defaults a score's lists to [] and drops a malformed gates list", () => {
    const report = parseEvalReport({
      ...good,
      ad_copy_evaluations: [{ original_id: 1, headline: "Go", score: { overall_score: 0.4, passed: false, gates: "x" } }],
    });
    const s = report?.ad_copy_evaluations[0].score;
    expect(s?.verdicts).toEqual([]);
    expect(s?.strengths).toEqual([]);
    expect(s?.improvements).toEqual([]);
    expect(s?.gates).toBeUndefined();
  });

  it("defaults the summary's numbers to 0 and its weakest list to []", () => {
    const report = parseEvalReport({ ...good, summary: { overall_pass_rate: "high" } });
    expect(report?.summary.overall_pass_rate).toBe(0);
    expect(report?.summary.total_ad_copies).toBe(0);
    expect(report?.summary.weakest_dimensions).toEqual([]);
  });

  it("drops non-array warnings and learning flags", () => {
    const report = parseEvalReport({ ...good, warnings: "one", learning_flags: { a: 1 } });
    expect(report?.warnings).toBeUndefined();
    expect(report?.learning_flags).toBeUndefined();
  });
});
