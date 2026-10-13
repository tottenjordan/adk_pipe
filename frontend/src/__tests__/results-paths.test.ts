import { describe, expect, it } from "vitest";
import { bucketFromState, outputDir } from "@/lib/results-paths";

describe("bucketFromState", () => {
  it("prefers gcs_bucket_name", () => {
    expect(bucketFromState({ gcs_bucket_name: "named", gcs_bucket: "gs://other" })).toBe("named");
  });

  it("falls back to gcs_bucket without its gs:// prefix", () => {
    expect(bucketFromState({ gcs_bucket: "gs://tt-bucket" })).toBe("tt-bucket");
    expect(bucketFromState({ gcs_bucket: "gs://tt-bucket/" })).toBe("tt-bucket");
    expect(bucketFromState({ gcs_bucket: "tt-bucket" })).toBe("tt-bucket");
  });

  it("is empty when neither key is a usable string", () => {
    expect(bucketFromState({})).toBe("");
    expect(bucketFromState({ gcs_bucket_name: "", gcs_bucket: 42 })).toBe("");
    expect(bucketFromState({ gcs_bucket_name: ["b"] })).toBe("");
  });
});

describe("outputDir", () => {
  const full = { gcs_bucket: "gs://tt-bucket", gcs_folder: "2026_10_13", agent_output_dir: "creative" };

  it("returns the bucket and the folder/subdir prefix", () => {
    expect(outputDir(full)).toEqual({ bucket: "tt-bucket", prefix: "2026_10_13/creative" });
  });

  it.each([
    ["no bucket", { gcs_folder: "f", agent_output_dir: "d" }],
    ["no folder", { gcs_bucket_name: "b", agent_output_dir: "d" }],
    ["no subdir", { gcs_bucket_name: "b", gcs_folder: "f" }],
    ["a non-string folder", { gcs_bucket_name: "b", gcs_folder: 7, agent_output_dir: "d" }],
  ])("is null with %s", (_label, state) => {
    expect(outputDir(state)).toBeNull();
  });
});
