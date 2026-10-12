// @vitest-environment jsdom
import { Suspense } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("app=creative_agent&userId=me"),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  listArtifacts: vi.fn(),
  getArtifact: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  ...api,
}));

import ResultsPage from "@/app/results/[sessionId]/page";

// The results page lists the run's artifacts by name only (image previews load
// from GCS), so it must not download every artifact's payload before rendering.

const session = (state: Record<string, unknown> = {}): Session =>
  ({ id: "s1", appName: "creative_agent", userId: "me", state, events: [] }) as unknown as Session;

function renderPage() {
  // Pre-fulfilled (React's own thenable fields), so `use(params)` reads it
  // synchronously instead of suspending.
  const params = Object.assign(Promise.resolve({ sessionId: "s1" }), {
    status: "fulfilled",
    value: { sessionId: "s1" },
  });
  return render(
    <Suspense fallback={null}>
      <ResultsPage params={params} />
    </Suspense>
  );
}

const fetchMock = vi.fn(async () => new Response("{}", { status: 404 }));

beforeEach(() => {
  api.getSession.mockReset();
  api.listArtifacts.mockReset();
  api.getArtifact.mockReset();
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("results page: artifacts", () => {
  it("lists artifact names without downloading any artifact payload", async () => {
    api.getSession.mockResolvedValue(session());
    api.listArtifacts.mockResolvedValue(["a.png", "b.pdf", "c.json"]);
    api.getArtifact.mockResolvedValue({ inlineData: { data: "xx" } });

    renderPage();

    await waitFor(() => expect(screen.queryByText("Loading results…")).toBeNull());
    expect(api.getArtifact).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Artifacts/ }));
    expect(screen.getByText("a.png", { selector: "p" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /PDFs/ }));
    expect(await screen.findByText("b.pdf")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Other/ }));
    expect(await screen.findByText("c.json")).toBeTruthy();
    expect(api.getArtifact).not.toHaveBeenCalled();
  });

  it("stops the load (no eval-report fetch) when unmounted mid-load", async () => {
    let resolveSession!: (s: Session) => void;
    api.getSession.mockReturnValue(new Promise<Session>((r) => (resolveSession = r)));
    api.listArtifacts.mockResolvedValue(["a.png"]);

    const { unmount } = renderPage();
    unmount();
    await act(async () => {
      resolveSession(
        // A finished run with an output folder: a mounted page would fetch the eval report.
        session({
          gcs_bucket_name: "b",
          gcs_folder: "f",
          agent_output_dir: "d",
          finalize_done: true,
        })
      );
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(api.getArtifact).not.toHaveBeenCalled();
  });
});
