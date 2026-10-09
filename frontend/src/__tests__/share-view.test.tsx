// @vitest-environment jsdom
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ShareView } from "@/components/share/share-view";
import { parseView } from "@/lib/share-views";
import { isShareChromeless } from "@/components/site-header";
import type { ShareSnapshotV1 } from "@/lib/share-snapshot";
import { SHARE_TOKEN, singleEvalSnapshot, slateSnapshot } from "./fixtures/share-snapshot";

afterEach(() => vi.restoreAllMocks());

const allImgs = (root: HTMLElement) => Array.from(root.querySelectorAll("img"));

describe("parseView", () => {
  it("defaults to card and accepts only known views", () => {
    expect(parseView(undefined)).toBe("card");
    expect(parseView("feed")).toBe("feed");
    expect(parseView("story")).toBe("story");
    expect(parseView("card")).toBe("card");
    expect(parseView("STORY")).toBe("card");
    expect(parseView(["story", "feed"])).toBe("story");
    expect(parseView("<script>")).toBe("card");
  });
});

describe("ShareView", () => {
  it.each(["card", "feed", "story"] as const)(
    "renders every creative's headline, body and CTA as text in the %s view",
    (view) => {
      const snap = slateSnapshot();
      const { container } = render(<ShareView snapshot={snap} initialView={view} />);
      const articles = container.querySelectorAll("article");
      expect(articles).toHaveLength(4);
      snap.creatives.forEach((c, i) => {
        const article = articles[i] as HTMLElement;
        const labelledBy = article.getAttribute("aria-labelledby");
        expect(labelledBy).toBeTruthy();
        const heading = container.querySelector(`#${labelledBy}`);
        expect(heading?.tagName).toBe("H2");
        expect(heading).toHaveTextContent(c.headline);
        expect(within(article).getAllByText(c.body).length).toBeGreaterThan(0);
        expect(within(article).getAllByText(c.cta).length).toBeGreaterThan(0);
      });
      // A slate is a list of articles.
      expect(container.querySelector("ul > li > article")).not.toBeNull();
    }
  );

  it.each(["card", "feed", "story"] as const)(
    "gives every image non-empty alt text and a token-scoped src in the %s view",
    (view) => {
      const snap = slateSnapshot();
      snap.creatives[1].alt = ""; // falls back to the headline
      const { container } = render(<ShareView snapshot={snap} initialView={view} />);
      const imgs = allImgs(container);
      expect(imgs.length).toBe(4);
      imgs.forEach((img, i) => {
        expect(img.getAttribute("alt")?.trim()).toBeTruthy();
        expect(img.getAttribute("src")).toBe(`/s/${SHARE_TOKEN}/img/${i}`);
      });
      expect(imgs[1].getAttribute("alt")).toBe(snap.creatives[1].headline);
    }
  );

  it("renders a single creative as one article without a list", () => {
    const { container } = render(<ShareView snapshot={singleEvalSnapshot()} initialView="card" />);
    expect(container.querySelectorAll("article")).toHaveLength(1);
    expect(container.querySelector("ul > li > article")).toBeNull();
  });

  it("shows the CTA as non-interactive text, not a link or button", () => {
    const snap = slateSnapshot();
    render(<ShareView snapshot={snap} initialView="card" />);
    const cta = screen.getByText(snap.creatives[0].cta);
    expect(cta.tagName).toBe("SPAN");
    expect(cta.closest("a, button")).toBeNull();
  });

  it("switches views with the keyboard and persists the view in ?view=", async () => {
    const replace = vi.spyOn(window.history, "replaceState");
    render(<ShareView snapshot={slateSnapshot()} initialView="card" />);
    const cardTab = screen.getByRole("tab", { name: "Card" });
    expect(cardTab).toHaveAttribute("aria-selected", "true");
    act(() => cardTab.focus());
    // Base UI moves focus in a microtask; activateOnFocus then selects the tab.
    await act(async () => {
      fireEvent.keyDown(cardTab, { key: "ArrowRight" });
    });
    const feedTab = screen.getByRole("tab", { name: "Feed" });
    expect(feedTab).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(feedTab);
    expect(screen.getAllByText("Sponsored").length).toBe(4);
    expect(replace).toHaveBeenLastCalledWith(null, "", expect.stringContaining("view=feed"));

    await act(async () => {
      fireEvent.keyDown(feedTab, { key: "ArrowRight" });
    });
    expect(screen.getByRole("tab", { name: "Story" })).toHaveAttribute("aria-selected", "true");
    expect(replace).toHaveBeenLastCalledWith(null, "", expect.stringContaining("view=story"));
  });

  it("hides the feed avatar from assistive tech and shows brand + Sponsored", () => {
    const { container } = render(<ShareView snapshot={slateSnapshot()} initialView="feed" />);
    const avatar = container.querySelector("[data-slot=feed-avatar]");
    expect(avatar).toHaveAttribute("aria-hidden", "true");
    expect(avatar).toHaveTextContent("P");
    expect(screen.getAllByText("PRS Guitars").length).toBeGreaterThan(0);
  });

  it("expands a truncated feed caption with a real button", () => {
    const snap = slateSnapshot();
    snap.creatives[0].caption = `${"word ".repeat(60)}END_OF_CAPTION`;
    render(<ShareView snapshot={snap} initialView="feed" />);
    expect(screen.queryByText(/END_OF_CAPTION/)).toBeNull();
    const more = screen.getAllByRole("button", { name: "more" })[0];
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(screen.getByText(/END_OF_CAPTION/)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: "more" })).toHaveLength(0);
  });

  it("does not offer 'more' for a short caption", () => {
    const snap = slateSnapshot();
    snap.creatives = [{ ...snap.creatives[0], caption: "Short." }];
    render(<ShareView snapshot={snap} initialView="feed" />);
    expect(screen.queryByRole("button", { name: "more" })).toBeNull();
  });

  it("repeats the story text outside the frame and hides the overlay from screen readers", () => {
    const snap = slateSnapshot();
    const { container } = render(<ShareView snapshot={snap} initialView="story" />);
    const overlay = container.querySelector("[data-slot=story-overlay]");
    expect(overlay).toHaveAttribute("aria-hidden", "true");
    expect(overlay).toHaveTextContent(snap.creatives[0].headline);
    const frame = container.querySelector("[data-slot=story-frame]") as HTMLElement;
    const article = frame.closest("article") as HTMLElement;
    const outside = Array.from(article.querySelectorAll("h2, p, span")).filter(
      (el) => !frame.contains(el)
    );
    const text = outside.map((el) => el.textContent).join(" ");
    for (const field of ["headline", "body", "caption", "cta"] as const) {
      expect(text).toContain(snap.creatives[0][field]);
    }
  });

  it("labels copy and visual checks and shows a null verdict as not judged", () => {
    const snap = singleEvalSnapshot();
    const e = snap.creatives[0].eval! as unknown as Record<string, unknown>;
    e.passed = null;
    e.checks = [
      { kind: "copy", gate: "avoid_respected", label: "Avoid list respected", passed: true, advisory: false },
      { kind: "visual", gate: "avoid_respected", label: "Avoid list respected", passed: false, advisory: false },
    ];
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<ShareView snapshot={snap} initialView="card" />);
    expect(screen.getByText("Not judged")).toBeInTheDocument();
    expect(screen.getByText("Copy: Avoid list respected")).toBeInTheDocument();
    expect(screen.getByText("Visual: Avoid list respected")).toBeInTheDocument();
    expect(err.mock.calls.some((c) => String(c[0]).includes("same key"))).toBe(false);
  });

  it("hides the eval section unless the snapshot includes it", () => {
    render(<ShareView snapshot={slateSnapshot()} initialView="card" />);
    expect(screen.queryByText("Checks")).toBeNull();
    expect(screen.queryByText(/advisory/i)).toBeNull();
  });

  it("ignores stray eval data when include_eval is false", () => {
    const snap = singleEvalSnapshot();
    render(<ShareView snapshot={{ ...snap, include_eval: false }} initialView="card" />);
    expect(screen.queryByText("Checks")).toBeNull();
  });

  it("shows checks with text pass/fail marks, advisory labels and the score", () => {
    render(<ShareView snapshot={singleEvalSnapshot()} initialView="card" />);
    expect(screen.getByText("Checks")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    const product = rows.find((r) => r.textContent?.includes("Product visible"))!;
    expect(within(product).getByText("pass")).toHaveClass("text-mark-pass");
    const text = rows.find((r) => r.textContent?.includes("In-image text correct"))!;
    expect(within(text).getByText("fail")).toHaveClass("text-mark-fail");
    const cue = rows.find((r) => r.textContent?.includes("Brand cue present"))!;
    expect(within(cue).getByText("advisory")).toBeInTheDocument();
    expect(screen.getByText("83%")).toBeInTheDocument();
    expect(screen.getByText("Did not pass")).toBeInTheDocument();
  });

  it("renders only allowlisted fields, never prompts or rationales", () => {
    const snap = slateSnapshot() as ShareSnapshotV1 & Record<string, unknown>;
    const leaky = {
      ...snap,
      session_id: "SESSION-123",
      creatives: snap.creatives.map((c) => ({
        ...c,
        image_generation_prompt: "SECRET PROMPT",
        rationale: "SECRET RATIONALE",
        note: "SECRET NOTE",
      })),
    } as ShareSnapshotV1;
    for (const view of ["card", "feed", "story"] as const) {
      const { container, unmount } = render(<ShareView snapshot={leaky} initialView={view} />);
      expect(container.textContent).not.toMatch(/SECRET|SESSION-123/);
      expect(container.innerHTML).not.toMatch(/SECRET|SESSION-123/);
      unmount();
    }
  });
});

describe("isShareChromeless", () => {
  it("drops the internal header and nav on every /s/ page", () => {
    expect(isShareChromeless(`/s/${SHARE_TOKEN}`)).toBe(true);
    expect(isShareChromeless("/s/whatever")).toBe(true);
    expect(isShareChromeless("/s")).toBe(true);
    expect(isShareChromeless("/")).toBe(false);
    expect(isShareChromeless("/runs")).toBe(false);
    expect(isShareChromeless("/sessions")).toBe(false);
    expect(isShareChromeless(null)).toBe(false);
  });
});
