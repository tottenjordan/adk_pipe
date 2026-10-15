import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";

// Tailwind v4: `outline-none` really removes the outline, so ring-only focus
// styles vanish in forced-colors (Windows high-contrast) mode. Use
// `outline-hidden` (transparent outline, shown in forced colors).
// https://tailwindcss.com/docs/upgrade-guide#renamed-outline-utility
const SRC = path.resolve(__dirname, "..");

describe("focus styles", () => {
  it("never use outline-none", () => {
    const offenders = (readdirSync(SRC, { recursive: true }) as string[])
      .filter((f) => /\.tsx?$/.test(f) && !f.includes("__tests__"))
      .filter((f) => /\boutline-none\b/.test(readFileSync(path.join(SRC, f), "utf8")));
    expect(offenders).toEqual([]);
  });
});
