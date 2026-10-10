// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ProofImage } from "@/app/results/[sessionId]/proof-grid";

describe("ProofImage", () => {
  it("shows the image while it loads", () => {
    render(<ProofImage src="/api/gcs?uri=a" alt="A creative" />);
    expect(screen.getByRole("img", { name: "A creative" })).toHaveAttribute("src", "/api/gcs?uri=a");
  });

  it("falls back to 'Image not rendered' without a src or when an uncast image fails", () => {
    const { rerender } = render(<ProofImage src={null} alt="A" />);
    expect(screen.getByText("Image not rendered")).toBeInTheDocument();
    rerender(<ProofImage src="/api/gcs?uri=b" alt="B" />);
    fireEvent.error(screen.getByRole("img", { name: "B" }));
    expect(screen.getByText("Image not rendered")).toBeInTheDocument();
  });

  it("says a cast image was removed when it no longer loads (consent revoked)", () => {
    render(<ProofImage src="/api/gcs?uri=c" alt="C" cast />);
    fireEvent.error(screen.getByRole("img", { name: "C" }));
    expect(screen.getByText("Image removed (consent revoked)")).toBeInTheDocument();
    expect(screen.queryByText("Image not rendered")).not.toBeInTheDocument();
  });

  it("keeps 'Image not rendered' for a cast creative that was never rendered", () => {
    render(<ProofImage src={null} alt="D" cast />);
    expect(screen.getByText("Image not rendered")).toBeInTheDocument();
  });
});
