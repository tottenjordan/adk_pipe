// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AppError from "@/app/error";
import ShareError from "@/app/s/[token]/error";

describe("route error boundaries", () => {
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    ["app", AppError],
    ["share", ShareError],
  ])("%s error boundary offers a retry and hides internals", (_n, Boundary) => {
    const retry = vi.fn();
    render(
      <Boundary
        error={Object.assign(new Error("gs://secret/path failed"), { digest: "abc123" })}
        retry={retry}
      />
    );
    expect(screen.getByRole("heading")).toBeInTheDocument();
    expect(screen.queryByText(/gs:\/\/secret/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("app boundary shows the digest as a reference; the public share boundary does not", () => {
    const error = Object.assign(new Error("boom"), { digest: "abc123" });
    const { unmount } = render(<AppError error={error} retry={() => {}} />);
    expect(screen.getByText(/Reference: abc123/)).toBeInTheDocument();
    unmount();
    render(<ShareError error={error} retry={() => {}} />);
    expect(screen.queryByText(/abc123/)).not.toBeInTheDocument();
  });
});
