// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { startTransition, use, useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Like Next's router.push: returns nothing and runs the navigation as a React
// transition that suspends until the new route's data lands (navigateTo below).
const push = vi.fn();
const search = new URLSearchParams({
  agent: "creative_agent",
  brand: "PRS",
  targetAudience: "guitarists",
  targetProduct: "SE CE24",
  keySellingPoints: "tone",
  targetSearchTrend: "World Series",
});
vi.mock("next/navigation", () => ({
  useSearchParams: () => search,
  useRouter: () => ({ push, replace: vi.fn() }),
}));

const createSession = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  createSession: (...args: unknown[]) => createSession(...args),
}));

import Home from "@/app/page";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// A stand-in for the router: navigateTo(p) commits a state update that suspends
// on p inside startTransition, so the navigation is pending until p resolves.
let navigateTo: (p: Promise<void>) => void = () => {};
function Navigation({ pending }: { pending: Promise<void> | null }) {
  if (pending) use(pending);
  return null;
}
function App() {
  const [pending, setPending] = useState<Promise<void> | null>(null);
  useEffect(() => {
    navigateTo = (p) => startTransition(() => setPending(p));
  }, []);
  return (
    <>
      <Home />
      <Navigation pending={pending} />
    </>
  );
}

let navigation: ReturnType<typeof deferred<void>>;

beforeEach(() => {
  navigation = deferred<void>();
  push.mockReset();
  push.mockImplementation(() => navigateTo(navigation.promise));
  createSession.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }))
  );
});
afterEach(() => vi.unstubAllGlobals());

const submitButton = () => screen.getByRole("button", { name: /Generate|Start|Starting/ });

describe("campaign form submit", () => {
  it("stays disabled while navigating, so a second click starts no second session", async () => {
    createSession.mockResolvedValue({ id: "s-1" });
    render(<App />);
    await waitFor(() => expect(submitButton()).toBeEnabled());

    fireEvent.click(submitButton());
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    expect(push.mock.calls[0][0]).toMatch(/^\/run\/s-1\?/);
    expect(JSON.parse(sessionStorage.getItem("run:s-1")!).message).toContain('Brand Name: "PRS"');

    // router.push has not unmounted the page yet: the button must stay busy.
    expect(submitButton()).toBeDisabled();
    expect(submitButton()).toHaveTextContent("Starting…");
    fireEvent.click(submitButton());
    fireEvent.submit(submitButton().closest("form")!);
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("is pending until the navigation resolves, not just until push is called", async () => {
    createSession.mockResolvedValue({ id: "s-1" });
    render(<App />);
    await waitFor(() => expect(submitButton()).toBeEnabled());

    fireEvent.click(submitButton());
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    expect(submitButton()).toBeDisabled();

    // In the app the new route replaces this page here; in the test the page
    // stays, which shows the busy state is tied to the navigation itself.
    await act(async () => navigation.resolve());
    await waitFor(() => expect(submitButton()).toBeEnabled());
  });

  it("ignores a second submit while the session is still being created", async () => {
    const session = deferred<{ id: string }>();
    createSession.mockReturnValue(session.promise);
    render(<App />);
    await waitFor(() => expect(submitButton()).toBeEnabled());

    fireEvent.click(submitButton());
    expect(submitButton()).toBeDisabled();
    fireEvent.click(submitButton());
    fireEvent.submit(submitButton().closest("form")!);
    fireEvent.keyDown(submitButton().closest("form")!, { key: "Enter", ctrlKey: true });
    expect(createSession).toHaveBeenCalledTimes(1);

    await act(async () => session.resolve({ id: "s-1" }));
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    expect(createSession).toHaveBeenCalledTimes(1);
  });

  it("re-enables submit after a failed start", async () => {
    const session = deferred<{ id: string }>();
    createSession.mockReturnValue(session.promise);
    render(<App />);
    await waitFor(() => expect(submitButton()).toBeEnabled());
    fireEvent.click(submitButton());
    expect(submitButton()).toBeDisabled();

    await act(async () => session.reject(new Error("offline")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't start the run: offline.");
    expect(submitButton()).toBeEnabled();
    expect(push).not.toHaveBeenCalled();
  });
});
