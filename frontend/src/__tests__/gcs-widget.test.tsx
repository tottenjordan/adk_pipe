// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GcsWidget } from "@/components/gcs-widget";

describe("GcsWidget", () => {
  it("links a gs:// folder to the Cloud Console browser", () => {
    render(<GcsWidget uri="gs://tt-bucket/2026_10_13/creative" />);
    expect(screen.getByRole("link", { name: "gs://tt-bucket/2026_10_13/creative" })).toHaveAttribute(
      "href",
      "https://console.cloud.google.com/storage/browser/tt-bucket/2026_10_13/creative"
    );
  });

  it("links a bare bucket", () => {
    render(<GcsWidget uri="gs://tt-bucket" />);
    expect(screen.getByRole("link", { name: "gs://tt-bucket" })).toHaveAttribute(
      "href",
      "https://console.cloud.google.com/storage/browser/tt-bucket/"
    );
  });

  it("shows a non-gs:// value as text, without a console link", () => {
    render(<GcsWidget uri="2026_10_13/creative" />);
    expect(screen.getByText("2026_10_13/creative")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("renders nothing without a uri", () => {
    const { container } = render(<GcsWidget uri="" />);
    expect(container).toBeEmptyDOMElement();
  });
});
