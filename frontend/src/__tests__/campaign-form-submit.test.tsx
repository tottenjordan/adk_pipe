// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

beforeEach(() => {
  push.mockReset();
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
    render(<Home />);
    await waitFor(() => expect(submitButton()).toBeEnabled());

    await act(async () => {
      fireEvent.click(submitButton());
    });
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    expect(push.mock.calls[0][0]).toMatch(/^\/run\/s-1\?/);

    // router.push has not unmounted the page yet: the button must stay busy.
    expect(submitButton()).toBeDisabled();
    await act(async () => {
      fireEvent.click(submitButton());
      fireEvent.submit(submitButton().closest("form")!);
    });
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("re-enables submit after a failed start", async () => {
    createSession.mockRejectedValue(new Error("offline"));
    render(<Home />);
    await waitFor(() => expect(submitButton()).toBeEnabled());
    await act(async () => {
      fireEvent.click(submitButton());
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't start the run: offline.");
    expect(submitButton()).toBeEnabled();
  });
});
