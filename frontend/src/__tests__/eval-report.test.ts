import { describe, it, expect, vi } from "vitest";
import { fetchEvalReport } from "@/lib/eval-report";

// The creative eval report is the LAST artifact the pipeline writes to GCS, so the
// results page can load (and fetch it) a few seconds before it exists — a 404 race.
// fetchEvalReport retries on 404 (the file is still being written) and reports a
// distinct "pending" outcome instead of silently swallowing the miss.

const noSleep = () => Promise.resolve();

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

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
