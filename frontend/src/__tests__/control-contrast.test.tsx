// @vitest-environment jsdom
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import { ExplainSwitch } from "@/components/explain";

const css = readFileSync(path.resolve(__dirname, "../app/globals.css"), "utf8");

function token(name: string): string {
  const m = css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!m) throw new Error(`--${name} is not a hex colour in globals.css`);
  return m[1];
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe("--control-off (WCAG 1.4.11 non-text contrast)", () => {
  it("is at least 3:1 against the card, the page and the hover fill", () => {
    const off = token("control-off");
    expect(contrast(off, token("card"))).toBeGreaterThanOrEqual(3);
    expect(contrast(off, token("background"))).toBeGreaterThanOrEqual(3);
    expect(contrast(off, token("muted"))).toBeGreaterThanOrEqual(3);
  });

  it("is what the Explain switch's off track uses (no hard-coded hex)", () => {
    render(<ExplainSwitch on={false} onChange={() => {}} />);
    const track = screen.getByRole("switch").querySelector("span[aria-hidden]");
    expect(track?.className).toContain("bg-control-off");
    expect(track?.className).not.toMatch(/bg-\[#/);
  });
});
