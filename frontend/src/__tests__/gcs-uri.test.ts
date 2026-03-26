import { describe, it, expect } from "vitest";

// Replicates GCS URI building logic from run page and results page.

function buildGcsUri(state: Record<string, unknown>): string {
  const parts = [state.gcs_bucket, state.gcs_folder, state.agent_output_dir].filter(Boolean);
  return parts.length >= 2 ? parts.join("/") : "";
}

function buildResearchReportUrl(uri: unknown): string {
  if (typeof uri !== "string" || !uri.startsWith("gs://")) return "";
  const withoutPrefix = uri.replace(/^gs:\/\//, "");
  const slashIdx = withoutPrefix.indexOf("/");
  if (slashIdx < 0) return "";
  const bucket = withoutPrefix.slice(0, slashIdx);
  const path = withoutPrefix.slice(slashIdx + 1);
  return `/api/gcs?bucket=${encodeURIComponent(bucket)}&path=${encodeURIComponent(path)}`;
}

describe("buildGcsUri", () => {
  it("joins all three parts", () => {
    const state = {
      gcs_bucket: "gs://my-bucket",
      gcs_folder: "campaigns/123",
      agent_output_dir: "output_2024",
    };
    expect(buildGcsUri(state)).toBe("gs://my-bucket/campaigns/123/output_2024");
  });

  it("joins two parts when one is missing", () => {
    const state = { gcs_bucket: "gs://bucket", gcs_folder: "folder" };
    expect(buildGcsUri(state)).toBe("gs://bucket/folder");
  });

  it("returns empty string when fewer than 2 parts", () => {
    expect(buildGcsUri({ gcs_bucket: "gs://bucket" })).toBe("");
    expect(buildGcsUri({})).toBe("");
  });

  it("filters out falsy values", () => {
    const state = { gcs_bucket: "gs://b", gcs_folder: "", agent_output_dir: "out" };
    expect(buildGcsUri(state)).toBe("gs://b/out");
  });
});

describe("buildResearchReportUrl", () => {
  it("converts gs:// URI to API proxy URL", () => {
    const url = buildResearchReportUrl("gs://my-bucket/path/to/report.pdf");
    expect(url).toBe("/api/gcs?bucket=my-bucket&path=path%2Fto%2Freport.pdf");
  });

  it("returns empty for non-gs:// strings", () => {
    expect(buildResearchReportUrl("https://example.com")).toBe("");
  });

  it("returns empty for non-string input", () => {
    expect(buildResearchReportUrl(null)).toBe("");
    expect(buildResearchReportUrl(undefined)).toBe("");
    expect(buildResearchReportUrl(42)).toBe("");
  });

  it("returns empty for gs:// with no path", () => {
    expect(buildResearchReportUrl("gs://bucket-only")).toBe("");
  });
});
