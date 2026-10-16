// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventLog } from "@/components/event-log";
import type { AgentEvent } from "@/lib/types";

const scrollIntoView = vi.fn();

function mockReducedMotion(reduce: boolean) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: reduce && query === "(prefers-reduced-motion: reduce)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

const events = [{ id: "e1", author: "brief_writer", content: { parts: [{ text: "Drafting" }] } }] as AgentEvent[];

beforeEach(() => {
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
});
afterEach(() => vi.restoreAllMocks());

describe("EventLog", () => {
  it("smooth-scrolls to the newest event", () => {
    mockReducedMotion(false);
    render(<EventLog events={events} />);
    expect(scrollIntoView).toHaveBeenLastCalledWith({ behavior: "smooth" });
  });

  it("jumps instead under prefers-reduced-motion", () => {
    mockReducedMotion(true);
    render(<EventLog events={events} />);
    expect(scrollIntoView).toHaveBeenLastCalledWith({ behavior: "auto" });
  });

  it("shows the shared processing dots and an ellipsis while waiting", () => {
    mockReducedMotion(false);
    const { container } = render(<EventLog events={[]} />);
    expect(screen.getByText("Waiting for events…")).toBeInTheDocument();
    expect(container.querySelector(".processing-dots")).not.toBeNull();
    expect(container.querySelector(".animate-bounce")).toBeNull();
  });
});
