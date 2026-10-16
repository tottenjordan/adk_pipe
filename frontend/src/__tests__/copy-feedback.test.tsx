// @vitest-environment jsdom
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Share } from "@/lib/shares";

// "Link copied" is visible next to the button that copied, announced politely,
// and cleared after about 2 s (People's folder Copy and each Shared links row).

const listShares = vi.fn();
const listPersonRefs = vi.fn();
vi.mock("@/lib/api", () => ({
  listShares: (...a: unknown[]) => listShares(...a),
  revokeShare: vi.fn(),
  listPersonRefs: (...a: unknown[]) => listPersonRefs(...a),
  createPersonRef: vi.fn(),
  revokePersonRef: vi.fn(),
}));

import { SessionShares } from "@/components/shares-list";
import PeoplePage from "@/app/people/page";

const share = (token: string, title: string): Share => ({
  token,
  url: `https://share.example.com/s/${token}`,
  title,
  scope: "slate",
  include_eval: false,
  concept_names: [],
  app_name: "creative_agent",
  session_id: "s1",
  created_at: "2026-10-09T10:00:00Z",
});

const writeText = vi.fn();
beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});
afterEach(() => vi.useRealTimers());

/** Flush the clipboard promise and the state update after it. */
async function click(button: HTMLElement) {
  await act(async () => {
    fireEvent.click(button);
  });
}

describe("visible copy confirmations", () => {
  it("shows 'Link copied' in the copied row only, then clears it", async () => {
    listShares.mockResolvedValue([share("AAAAAAAAAAAAAAAA", "First"), share("BBBBBBBBBBBBBBBB", "Second")]);
    render(<SessionShares sessionId="s1" />);
    const list = await screen.findByRole("list", { name: "Shared links" });
    const [first, second] = within(list).getAllByRole("listitem");
    vi.useFakeTimers();
    await click(within(second).getByRole("button", { name: "Copy link to Second" }));

    const status = within(second).getByText("Link copied");
    expect(status).toBeVisible();
    expect(status).not.toHaveClass("sr-only");
    expect(status.closest("[aria-live='polite']")).not.toBeNull();
    expect(within(first).queryByText("Link copied")).toBeNull();

    act(() => vi.advanceTimersByTime(1900));
    expect(within(second).getByText("Link copied")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(200));
    expect(within(second).queryByText("Link copied")).toBeNull();
  });

  it("shows People's folder copy next to its Copy button, then clears it", async () => {
    listPersonRefs.mockResolvedValue({ personRefs: [], prefix: "gs://b/person-refs/a/" });
    render(<PeoplePage />);
    const copy = await screen.findByRole("button", { name: "Copy" });
    vi.useFakeTimers();
    await click(copy);

    const status = screen.getByTestId("person-copy-status");
    expect(status).toHaveTextContent("Link copied");
    expect(status).not.toHaveClass("sr-only");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(copy.parentElement).toContainElement(status);

    act(() => vi.advanceTimersByTime(2100));
    expect(status).toHaveTextContent("");
  });
});
