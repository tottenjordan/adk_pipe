import { afterEach, describe, expect, it, vi } from "vitest";

async function loadConfig() {
  vi.resetModules();
  return (await import("../../next.config")).default;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("next.config allowedDevOrigins", () => {
  it("reads a trimmed, comma-separated ALLOWED_DEV_ORIGINS", async () => {
    vi.stubEnv("ALLOWED_DEV_ORIGINS", " 3000-me.example.dev , *.example.dev ,, ");
    expect((await loadConfig()).allowedDevOrigins).toEqual(["3000-me.example.dev", "*.example.dev"]);
  });

  it("omits the option when the variable is unset or blank", async () => {
    vi.stubEnv("ALLOWED_DEV_ORIGINS", "");
    expect(await loadConfig()).not.toHaveProperty("allowedDevOrigins");
    vi.stubEnv("ALLOWED_DEV_ORIGINS", " , ");
    expect(await loadConfig()).not.toHaveProperty("allowedDevOrigins");
  });

  it("commits no workstation hostname", async () => {
    vi.stubEnv("ALLOWED_DEV_ORIGINS", "");
    expect(JSON.stringify(await loadConfig())).not.toContain("cloudworkstations");
  });
});
