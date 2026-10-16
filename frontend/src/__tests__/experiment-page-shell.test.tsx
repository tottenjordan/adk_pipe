// @vitest-environment jsdom
import { Suspense } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SESSION_EXPIRED_MESSAGE } from "@/lib/session-expired";

// The experiment page's load shell: a hard load error offers a retry that
// restarts the status poll (and the session-expired message offers a reload).

const pollExperiment = vi.fn();
vi.mock("@/lib/experiments", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/experiments")>()),
  pollExperiment: (...a: unknown[]) => pollExperiment(...a),
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import ExperimentPage from "@/app/experiments/[experimentId]/page";

function renderPage() {
  const params = Object.assign(Promise.resolve({ experimentId: "exp-1" }), {
    status: "fulfilled",
    value: { experimentId: "exp-1" },
  });
  return render(
    <Suspense fallback={null}>
      <ExperimentPage params={params} searchParams={Promise.resolve({})} />
    </Suspense>
  );
}

/** A poll generator that fails at once with `err`. */
async function* failing(err: Error) {
  throw err;
}
/** A poll generator that never yields (still loading). */
async function* pending() {
  await new Promise(() => {});
}

beforeEach(() => pollExperiment.mockReset());

describe("experiment page load shell", () => {
  it("retries a hard load error by restarting the poll", async () => {
    pollExperiment
      .mockImplementationOnce(() => failing(new Error("Couldn't load the experiment (404)")))
      .mockImplementationOnce(() => pending());
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This experiment didn't load");
    expect(alert).toHaveTextContent("(404)");
    expect(within(alert).getByRole("link", { name: "Open experiments" })).toHaveAttribute(
      "href",
      "/experiments"
    );

    await act(async () => {
      fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    });
    expect(pollExperiment).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Loading experiment…");
  });

  it("offers a reload when the sign-in expired", async () => {
    pollExperiment.mockImplementation(() => failing(new Error(SESSION_EXPIRED_MESSAGE)));
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    try {
      renderPage();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(SESSION_EXPIRED_MESSAGE);
      fireEvent.click(within(alert).getByRole("button", { name: "Reload" }));
      expect(reload).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
