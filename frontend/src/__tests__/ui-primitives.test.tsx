// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ErrorState } from "@/components/ui/error-state";
import { Kbd } from "@/components/ui/kbd";
import { Skeleton } from "@/components/ui/skeleton";

describe("Skeleton", () => {
  it("is decorative and stops pulsing under reduced motion", () => {
    const { container } = render(<Skeleton className="h-4 w-20" />);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toHaveAttribute("aria-hidden");
    expect(el.className).toContain("animate-pulse");
    expect(el.className).toContain("motion-reduce:animate-none");
    expect(el.className).toContain("h-4");
  });
});

describe("ErrorState", () => {
  it("is announced and shows title and message", () => {
    render(<ErrorState title="Couldn't load runs" message="The server didn't answer." />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Couldn't load runs");
    expect(alert).toHaveTextContent("The server didn't answer.");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("calls onRetry from the retry button", () => {
    const onRetry = vi.fn();
    render(<ErrorState title="Failed" onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("renders the secondary link", () => {
    render(<ErrorState title="Failed" secondary={{ href: "/", label: "Start a new run" }} />);
    expect(screen.getByRole("link", { name: "Start a new run" })).toHaveAttribute("href", "/");
  });
});

describe("Kbd", () => {
  it("renders a kbd element", () => {
    render(<Kbd>?</Kbd>);
    expect(screen.getByText("?").tagName).toBe("KBD");
  });
});
