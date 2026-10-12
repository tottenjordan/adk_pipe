// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { VariantCompare } from "@/components/variant-compare";

const ORIGINAL = { src: "/api/gcs?bucket=b&path=run%2Fbase.png", alt: "Original: A guitarist", caption: "Original" };
const PREVIEW = {
  src: "/api/gcs?bucket=b&path=run%2Fvariants%2Fv.png",
  alt: "Personalised preview with Me: A guitarist",
  caption: "With Me",
};

describe("VariantCompare", () => {
  it("shows both thumbnails as buttons that open a large side-by-side view", async () => {
    render(<VariantCompare original={ORIGINAL} preview={PREVIEW} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "View larger: With Me" }));
    const dialog = await screen.findByRole("dialog", { name: "Compare previews" });

    expect(within(dialog).getByAltText(ORIGINAL.alt)).toHaveAttribute("src", ORIGINAL.src);
    expect(within(dialog).getByAltText(PREVIEW.alt)).toHaveAttribute("src", PREVIEW.src);

    const links = within(dialog).getAllByRole("link", { name: /Open full size/ });
    expect(links.map((a) => a.getAttribute("href"))).toEqual([ORIGINAL.src, PREVIEW.src]);
    for (const link of links) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
    expect(within(dialog).getByRole("link", { name: /Original \(opens in a new tab\)/ })).toBeInTheDocument();
  });

  it("opens from the original thumbnail too and closes on Escape", async () => {
    render(<VariantCompare original={ORIGINAL} preview={PREVIEW} />);
    fireEvent.click(screen.getByRole("button", { name: "View larger: Original" }));
    const dialog = await screen.findByRole("dialog", { name: "Compare previews" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("does not offer to enlarge or open a missing image", async () => {
    render(<VariantCompare original={{ ...ORIGINAL, src: null }} preview={PREVIEW} />);
    expect(screen.getByRole("button", { name: "View larger: Original" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "View larger: With Me" }));
    const dialog = await screen.findByRole("dialog", { name: "Compare previews" });
    expect(within(dialog).getAllByRole("link", { name: /Open full size/ })).toHaveLength(1);
    expect(within(dialog).getByText("Image not rendered")).toBeInTheDocument();
  });
});
