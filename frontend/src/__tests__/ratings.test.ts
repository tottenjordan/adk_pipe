import { afterEach, describe, expect, it, vi } from "vitest";
import type { Proof, VisualConcept, AdCopy } from "@/lib/eval-matching";
import {
  buildRatingPayload,
  creativeKeysFor,
  draftFrom,
  isDirty,
  isProofRated,
  optimisticRating,
  ratingsByKey,
  type Rating,
} from "@/lib/ratings";
import { getRatings, putRating } from "@/lib/api";

const proof = (adCopy?: Partial<AdCopy>): Proof => ({
  index: 0,
  concept: { concept_name: "The Jackpot Reveal", headline: "H", ad_copy_id: 3 } as VisualConcept,
  adCopy: adCopy as AdCopy | undefined,
});

const rating = (over: Partial<Rating> = {}): Rating => ({
  creative_key: "visual:The Jackpot Reveal",
  kind: "visual",
  verdict: "pass",
  score: null,
  note: null,
  updated_at: "2026-10-07T10:00:00+00:00",
  ...over,
});

describe("creativeKeysFor", () => {
  it("keys the visual by concept name and the paired copy by original_id", () => {
    expect(creativeKeysFor(proof({ original_id: 3 }))).toEqual({
      visual: "visual:The Jackpot Reveal",
      adCopy: "copy:3",
    });
  });
  it("has no copy key without a paired ad copy", () => {
    expect(creativeKeysFor(proof())).toEqual({ visual: "visual:The Jackpot Reveal" });
  });
});

describe("buildRatingPayload", () => {
  it("needs a verdict", () => {
    expect(buildRatingPayload("creative_agent", "copy:3", "ad_copy", draftFrom())).toBeNull();
  });
  it("builds the PUT body, trimming the note and nulling a blank one", () => {
    expect(
      buildRatingPayload("creative_agent", "copy:3", "ad_copy", {
        verdict: "fail",
        score: 2,
        note: "  off-brand  ",
      })
    ).toEqual({
      app_name: "creative_agent",
      creative_key: "copy:3",
      kind: "ad_copy",
      verdict: "fail",
      score: 2,
      note: "off-brand",
    });
    expect(
      buildRatingPayload("creative_agent", "visual:X", "visual", { verdict: "pass", score: null, note: " " })
        ?.note
    ).toBeNull();
  });
  it("rejects an out-of-range score or an over-long note", () => {
    const base = { verdict: "pass" as const, note: "" };
    expect(buildRatingPayload("a", "visual:X", "visual", { ...base, score: 0 })).toBeNull();
    expect(buildRatingPayload("a", "visual:X", "visual", { ...base, score: 2.5 })).toBeNull();
    expect(
      buildRatingPayload("a", "visual:X", "visual", { ...base, score: null, note: "x".repeat(2001) })
    ).toBeNull();
  });
});

describe("draft helpers", () => {
  it("starts from the saved rating and tracks changes", () => {
    const saved = rating({ score: 4, note: "ok" });
    const d = draftFrom(saved);
    expect(d).toEqual({ verdict: "pass", score: 4, note: "ok" });
    expect(isDirty(d, saved)).toBe(false);
    expect(isDirty({ ...d, note: "ok  " }, saved)).toBe(false);
    expect(isDirty({ ...d, verdict: "fail" }, saved)).toBe(true);
    expect(isDirty({ ...d, score: null }, saved)).toBe(true);
    expect(isDirty(draftFrom(), undefined)).toBe(false);
  });
  it("optimistic rating carries the payload over the previous rating", () => {
    const prev = rating({ rating_id: "r1", judge_overall: 0.8 });
    const next = optimisticRating(
      { app_name: "a", creative_key: prev.creative_key, kind: "visual", verdict: "fail", score: 1, note: null },
      prev
    );
    expect(next).toMatchObject({ rating_id: "r1", judge_overall: 0.8, verdict: "fail", score: 1 });
    expect(next.updated_at).not.toBe(prev.updated_at);
  });
});

describe("ratingsByKey / isProofRated", () => {
  it("keeps the newest rating per key and skips junk", () => {
    const old = rating({ verdict: "fail", updated_at: "2026-10-01T00:00:00+00:00" });
    const map = ratingsByKey([old, rating(), null as unknown as Rating]);
    expect(map["visual:The Jackpot Reveal"].verdict).toBe("pass");
  });
  it("counts a proof as rated from either its visual or its copy", () => {
    const p = proof({ original_id: 3 });
    expect(isProofRated(p, {})).toBe(false);
    expect(isProofRated(p, { "copy:3": rating({ creative_key: "copy:3", kind: "ad_copy" }) })).toBe(true);
    expect(isProofRated(p, ratingsByKey([rating()]))).toBe(true);
    expect(isProofRated(proof(), { "copy:3": rating({ creative_key: "copy:3" }) })).toBe(false);
  });
});

describe("ratings REST client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("PUTs to the placeholder user path with the JSON body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(rating()), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const body = { app_name: "a", creative_key: "visual:X", kind: "visual" as const, verdict: "pass" as const, score: null, note: null };
    await putRating("sess-1", body);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/adk/ratings/me/sess-1");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual(body);
  });

  it("throws on a failed save and lists ratings defensively", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 502 })));
    await expect(
      putRating("s", { app_name: "a", creative_key: "visual:X", kind: "visual", verdict: "pass", score: null, note: null })
    ).rejects.toThrow("502");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 200 })));
    expect(await getRatings("s")).toEqual([]);
  });
});
