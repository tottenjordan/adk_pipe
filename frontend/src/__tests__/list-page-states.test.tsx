// @vitest-environment jsdom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SessionExpiredError } from "@/lib/session-expired";
import type { Session } from "@/lib/types";
import type { ExperimentSummary } from "@/lib/experiments";

// Skeleton → data, error → retry → data, and the session-expired message, for
// every list page (and the New-run page's recent-runs aside).

const listSessions = vi.fn();
const listPersonRefs = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  listSessions: (...a: unknown[]) => listSessions(...a),
  listPersonRefs: (...a: unknown[]) => listPersonRefs(...a),
  getCalibration: () => new Promise(() => {}),
}));

const listExperiments = vi.fn();
vi.mock("@/lib/experiments", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/experiments")>()),
  listExperiments: (...a: unknown[]) => listExperiments(...a),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import RunsPage from "@/app/runs/page";
import ExperimentsPage from "@/app/experiments/page";
import PeoplePage from "@/app/people/page";
import { RecentRunsAside } from "@/components/new-run/recent-runs-aside";

const session = (id: string, brand: string): Session =>
  ({
    id,
    appName: "creative_agent",
    userId: "me",
    lastUpdateTime: 1_760_000_000,
    state: { agent: "creative_agent", brand },
    events: [],
  }) as unknown as Session;

const experiment = (id: string): ExperimentSummary =>
  ({
    experimentId: id,
    scenario: "stable",
    status: "ready",
    createdAt: "2026-10-15T10:00:00Z",
    ttlExpiresAt: null,
    arms: [{ index: 0, conceptName: "Jackpot rig" }],
  }) as unknown as ExperimentSummary;

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  listSessions.mockReset();
  listPersonRefs.mockReset();
  listExperiments.mockReset();
});

type Case = {
  name: string;
  render: () => void;
  mock: ReturnType<typeof vi.fn>;
  data: unknown;
  loading: string;
  shown: string;
  failure: RegExp;
};

const CASES: Case[] = [
  {
    name: "/runs",
    render: () => render(<RunsPage />),
    mock: listSessions,
    data: [session("s-1", "PRS Guitars")],
    loading: "Loading runs…",
    shown: "PRS Guitars",
    failure: /Couldn't load runs/,
  },
  {
    name: "recent runs aside",
    render: () => render(<RecentRunsAside onDuplicate={() => {}} />),
    mock: listSessions,
    data: [session("s-1", "PRS Guitars")],
    loading: "Loading runs…",
    shown: "PRS Guitars",
    failure: /Couldn't load runs/,
  },
  {
    name: "/experiments",
    render: () => render(<ExperimentsPage />),
    mock: listExperiments,
    data: [experiment("exp-1")],
    loading: "Loading experiments…",
    shown: "1 creative:",
    failure: /Couldn't load experiments/,
  },
  {
    name: "/people",
    render: () => render(<PeoplePage />),
    mock: listPersonRefs,
    data: {
      personRefs: [
        {
          consent_id: "cid_AAAAAAAAAAAA",
          photo_uri: "gs://b/person-refs/a/me.jpg",
          label: "Alice",
          subject: "self",
          adult_attested: true,
          allow_public_share: false,
          consent_text_version: "v1",
          created_at: "2026-10-09T10:00:00Z",
        },
      ],
      prefix: null,
    },
    loading: "Loading people…",
    shown: "Alice",
    failure: /Couldn't load people/,
  },
];

describe.each(CASES)("$name load states", (c) => {
  it("shows layout skeletons, then the data", async () => {
    const d = deferred<unknown>();
    c.mock.mockReturnValue(d.promise);
    c.render();
    const status = screen.getByText(c.loading).closest('[role="status"]');
    expect(status).not.toBeNull();
    expect(status!.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0);
    d.resolve(c.data);
    expect(await screen.findByText(c.shown, { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(c.loading)).toBeNull();
  });

  it("offers a retry after a failure, which loads the data", async () => {
    c.mock.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce(c.data);
    c.render();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(c.failure);
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(c.shown, { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(c.mock).toHaveBeenCalledTimes(2);
  });

  it("says the sign-in expired, with a reload instead of a retry", async () => {
    c.mock.mockRejectedValue(new SessionExpiredError());
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    try {
      c.render();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("Your sign-in expired. Reload to continue.");
      expect(within(alert).queryByRole("button", { name: "Try again" })).toBeNull();
      fireEvent.click(within(alert).getByRole("button", { name: "Reload" }));
      expect(reload).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
