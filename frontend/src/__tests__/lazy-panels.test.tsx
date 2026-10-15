// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import path from "node:path";
import { Suspense } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";

// The research report (react-markdown + remark-gfm) and the Deploy panel (bandit
// scenario + reader tuning code) are heavy and rarely needed on first paint, so
// both load through `next/dynamic` instead of a static import.

const SRC = path.resolve(__dirname, "..");
const LAZY_FILES = ["app/results/[sessionId]/page.tsx", "app/run/[sessionId]/report-editor.tsx"];
const HEAVY_MODULES = ["@/components/research-report", "react-markdown", "./deploy-panel"];

/** The module specifiers of a file's static value imports (type-only imports are erased). */
function staticImports(file: string): string[] {
  const src = readFileSync(path.join(SRC, file), "utf8");
  const specs: string[] = [];
  for (const m of src.matchAll(/^import\s+(?!type\s)[^;]*?from\s+["']([^"']+)["']/gm)) {
    specs.push(m[1]);
  }
  return specs;
}

describe("lazy panels: no static heavy imports", () => {
  it.each(LAZY_FILES)("%s imports no heavy panel statically", (file) => {
    const specs = staticImports(file);
    // Sanity: the parser sees the file's ordinary imports.
    expect(specs).toContain("react");
    for (const heavy of HEAVY_MODULES) expect(specs).not.toContain(heavy);
  });

  it.each(LAZY_FILES)("%s loads the research report through next/dynamic", (file) => {
    const src = readFileSync(path.join(SRC, file), "utf8");
    expect(src).toMatch(/^import dynamic from "next\/dynamic";$/m);
    expect(src).toMatch(/dynamic\(\s*\(\)\s*=>\s*import\("@\/components\/research-report"\)/);
  });

  it("the results page loads the Deploy panel through next/dynamic", () => {
    const src = readFileSync(path.join(SRC, "app/results/[sessionId]/page.tsx"), "utf8");
    expect(src).toMatch(/dynamic\(\s*\(\)\s*=>\s*import\("\.\/deploy-panel"\)/);
  });
});

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  listArtifacts: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

import ResultsPage from "@/app/results/[sessionId]/page";

function renderPage() {
  // Pre-fulfilled (React's own thenable fields), so `use(params)` reads it synchronously.
  const params = Object.assign(Promise.resolve({ sessionId: "s1" }), {
    status: "fulfilled",
    value: { sessionId: "s1" },
  });
  return render(
    <Suspense fallback={null}>
      <ResultsPage params={params} searchParams={Promise.resolve({})} />
    </Suspense>
  );
}

beforeEach(() => {
  api.getSession.mockReset();
  api.listArtifacts.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 404 }))
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("lazy panels: results page research report", () => {
  it("mounts the report only once its disclosure is opened", async () => {
    api.getSession.mockResolvedValue({
      id: "s1",
      appName: "creative_agent",
      userId: "me",
      state: { combined_final_cited_report: "## Market pulse\n\nLazy-loaded report body." },
      events: [],
    } as unknown as Session);
    api.listArtifacts.mockResolvedValue([]);

    renderPage();
    await waitFor(() => expect(screen.queryByText("Loading results…")).toBeNull());
    // Closed disclosure: the report is not mounted (so its chunk is not requested).
    expect(screen.queryByText("Lazy-loaded report body.")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Research report/ }));
    expect(await screen.findByText("Lazy-loaded report body.")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Market pulse" })).toBeTruthy();
  });
});
