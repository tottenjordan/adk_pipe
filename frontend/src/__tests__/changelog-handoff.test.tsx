// @vitest-environment jsdom
import React, { Suspense, act } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RunChangelog } from "@/lib/api";

const replaceMock = vi.fn();
const pushMock = vi.fn();
const routerMock = { replace: replaceMock, push: pushMock };
let currentSearchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
  useSearchParams: () => currentSearchParams,
}));

const getSessionMock = vi.fn();
const listArtifactsMock = vi.fn();
const fetchRunChangelogMock = vi.fn();
const claimRunHandoffMock = vi.fn();
const getRatingsMock = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getSession: (...a: unknown[]) => getSessionMock(...a),
    listArtifacts: (...a: unknown[]) => listArtifactsMock(...a),
    fetchRunChangelog: (...a: unknown[]) => fetchRunChangelogMock(...a),
    claimRunHandoff: (...a: unknown[]) => claimRunHandoffMock(...a),
    getRatings: (...a: unknown[]) => getRatingsMock(...a),
  };
});

import ResultsPage from "@/app/results/[sessionId]/page";
import RunPage from "@/app/run/[sessionId]/page";

const SAMPLE_CHANGELOG: RunChangelog = {
  session_id: "claimed-sid-77",
  app_name: "creative_agent",
  origin: "gemini_enterprise",
  handoff_source_session_id: "a2a-sid-1",
  gate_rounds: { brief: 1, ad_copy: 2, visual_concept: 0 },
  source_counts: { web: 4, enterprise: 2 },
  warnings: ["Image check has unresolved issues: 1 (e.g. minor kerning)"],
  entries: [
    {
      kind: "origin",
      label: "Started in Gemini Enterprise",
      detail: "Handed off from Gemini Enterprise session a2a-sid-1",
    },
    {
      kind: "research",
      label: "Synthesized research & creative brief",
      detail: "4 web source(s), 2 enterprise source(s), brief gate revised 1x",
    },
  ],
};

beforeEach(() => {
  replaceMock.mockReset();
  pushMock.mockReset();
  getSessionMock.mockReset();
  listArtifactsMock.mockReset();
  fetchRunChangelogMock.mockReset();
  claimRunHandoffMock.mockReset();
  getRatingsMock.mockReset();
  getRatingsMock.mockResolvedValue([]);
  listArtifactsMock.mockResolvedValue([]);
});

describe("ResultsPage Gemini Enterprise handoff & Run changelog", () => {
  it("redeems a one-time handoff token and replaces the URL without &handoff", async () => {
    currentSearchParams = new URLSearchParams(
      "app=creative_agent&from=ge&handoff=tok-abc.sig-xyz"
    );
    claimRunHandoffMock.mockResolvedValue({
      session_id: "claimed-sid-77",
      app_name: "creative_agent",
      already_owned: false,
    });

    const paramsPromise = Promise.resolve({ sessionId: "a2a-sid-1" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(claimRunHandoffMock).toHaveBeenCalledTimes(1);
    });
    expect(claimRunHandoffMock).toHaveBeenCalledWith(
      "creative_agent",
      "me",
      "a2a-sid-1",
      "tok-abc.sig-xyz",
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );
    expect(replaceMock).toHaveBeenCalledWith(
      "/results/claimed-sid-77?app=creative_agent&from=ge"
    );
    // Does not prematurely fetch the unowned a2a-service session
    expect(getSessionMock).not.toHaveBeenCalled();
  });

  it("aborts in-flight handoff on unmount and re-redeems cleanly on StrictMode remount", async () => {
    currentSearchParams = new URLSearchParams(
      "app=creative_agent&from=ge&handoff=tok-strict.sig"
    );
    const signals: AbortSignal[] = [];
    let resolveSecond!: (v: {
      session_id: string;
      app_name: string;
      already_owned: boolean;
    }) => void;

    claimRunHandoffMock.mockImplementationOnce(
      (_app: string, _u: string, _sid: string, _tok: string, opts?: { signal?: AbortSignal }) => {
        if (opts?.signal) signals.push(opts.signal);
        return new Promise((_res, rej) => {
          opts?.signal?.addEventListener("abort", () => {
            const abortErr = new Error("Aborted");
            abortErr.name = "AbortError";
            rej(abortErr);
          });
        });
      }
    );
    claimRunHandoffMock.mockImplementationOnce(
      (_app: string, _u: string, _sid: string, _tok: string, opts?: { signal?: AbortSignal }) => {
        if (opts?.signal) signals.push(opts.signal);
        return new Promise((res) => {
          resolveSecond = res;
        });
      }
    );

    const p1 = Promise.resolve({ sessionId: "a2a-sid-strict" });
    let unmountFirst!: () => void;
    await act(async () => {
      const rendered = render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={p1} searchParams={Promise.resolve({})} />
        </Suspense>
      );
      unmountFirst = rendered.unmount;
    });
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);

    // Unmount first mount -> aborts signal without navigating or surfacing an error
    await act(async () => {
      unmountFirst();
    });
    expect(signals[0].aborted).toBe(true);
    expect(replaceMock).not.toHaveBeenCalled();

    // Second mount (StrictMode remount) -> redeems and navigates
    const p2 = Promise.resolve({ sessionId: "a2a-sid-strict" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={p2} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });
    expect(signals).toHaveLength(2);
    await act(async () => {
      resolveSecond({
        session_id: "claimed-strict-88",
        app_name: "creative_agent",
        already_owned: false,
      });
    });
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith(
        "/results/claimed-strict-88?app=creative_agent&from=ge"
      );
    });
  });

  it("keeps Run changelog collapsed by default on Studio runs and lazily loads on open", async () => {
    currentSearchParams = new URLSearchParams("app=creative_agent");
    getSessionMock.mockResolvedValue({
      id: "studio-sid-1",
      appName: "creative_agent",
      userId: "me",
      state: {
        brand: "PRS Guitars",
        target_search_trends: "acoustic session",
        finalize_done: true,
      },
      events: [],
    });
    fetchRunChangelogMock.mockResolvedValue({
      ...SAMPLE_CHANGELOG,
      session_id: "studio-sid-1",
      origin: "studio",
      handoff_source_session_id: null,
    });

    const paramsPromise = Promise.resolve({ sessionId: "studio-sid-1" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Results" })).toBeInTheDocument();
    });
    expect(
      screen.queryByText("Started in Gemini Enterprise")
    ).not.toBeInTheDocument();
    // Lazy: not fetched until the disclosure is opened
    expect(fetchRunChangelogMock).not.toHaveBeenCalled();

    const trigger = screen.getByRole("button", { name: /Run changelog/i });
    fireEvent.click(trigger);

    await waitFor(() => {
      expect(fetchRunChangelogMock).toHaveBeenCalledWith(
        "creative_agent",
        "me",
        "studio-sid-1",
        expect.objectContaining({ signal: expect.any(AbortSignal) })
      );
    });
    expect(await screen.findByText("4 web · 2 enterprise")).toBeInTheDocument();
    expect(
      screen.getByText(/Brief 1 · Copy 2 · Visual 0/)
    ).toBeInTheDocument();
    expect(
      screen.getByText("Synthesized research & creative brief")
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Image check has unresolved issues/)
    ).toBeInTheDocument();
  });

  it("renders the 'Started in Gemini Enterprise' provenance badge when from=ge", async () => {
    currentSearchParams = new URLSearchParams("app=creative_agent&from=ge");
    getSessionMock.mockResolvedValue({
      id: "claimed-sid-77",
      appName: "creative_agent",
      userId: "me",
      state: {
        run_origin: "gemini_enterprise",
        handoff_source_session_id: "a2a-sid-1",
        brand: "PRS Guitars",
        finalize_done: true,
      },
      events: [],
    });
    fetchRunChangelogMock.mockResolvedValue(SAMPLE_CHANGELOG);

    const paramsPromise = Promise.resolve({ sessionId: "claimed-sid-77" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <ResultsPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    expect(
      await screen.findByText("Started in Gemini Enterprise")
    ).toBeInTheDocument();
  });

  it("redeems a handoff token on /run/[sessionId] and stays on /run for checkpoint review", async () => {
    currentSearchParams = new URLSearchParams(
      "app=interactive_creative&from=ge&handoff=run-tok.sig"
    );
    claimRunHandoffMock.mockResolvedValue({
      session_id: "claimed-interactive-9",
      app_name: "interactive_creative",
      already_owned: false,
    });

    const paramsPromise = Promise.resolve({ sessionId: "a2a-paused-1" });
    await act(async () => {
      render(
        <Suspense fallback={<div>Loading…</div>}>
          <RunPage params={paramsPromise} searchParams={Promise.resolve({})} />
        </Suspense>
      );
    });

    await waitFor(() => {
      expect(claimRunHandoffMock).toHaveBeenCalledWith(
        "interactive_creative",
        "me",
        "a2a-paused-1",
        "run-tok.sig",
        expect.objectContaining({ signal: expect.any(AbortSignal) })
      );
    });
    expect(replaceMock).toHaveBeenCalledWith(
      "/run/claimed-interactive-9?app=interactive_creative&from=ge"
    );
  });
});
