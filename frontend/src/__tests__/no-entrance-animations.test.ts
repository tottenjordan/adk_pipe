import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";

// Proof-room rule: no entrance animations. shadcn's dialog/select templates ship
// tw-animate-css enter/exit classes; this keeps them from coming back on a re-add.
const ANIMATION_CLASS = /(?:^|[\s":])(?:animate-(?:in|out|none)|fade-(?:in|out)-\d+|zoom-(?:in|out)-\d+|slide-(?:in|out)-from-[a-z-]+\d*)\b/;

describe.each(["dialog.tsx", "select.tsx"])("ui/%s", (file) => {
  it("has no enter/exit animation classes", () => {
    const src = readFileSync(path.resolve(__dirname, "../components/ui", file), "utf8");
    expect(src).not.toMatch(ANIMATION_CLASS);
  });
});
