// @vitest-environment jsdom
import { use } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The New-run form's accessibility: required fields say so, a missing field is
// flagged and described once left (or on a blocked Ctrl/⌘+Enter), the
// disabled-submit hint is tied to the button, and the page never renders blank.

let search = new URLSearchParams();
let suspend = false;
const never = new Promise<never>(() => {});
vi.mock("next/navigation", () => ({
  useSearchParams: () => {
    if (suspend) use(never);
    return search;
  },
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const createSession = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  createSession: (...args: unknown[]) => createSession(...args),
}));

import Home from "@/app/page";
import { submitLabel } from "@/lib/agents";

beforeEach(() => {
  search = new URLSearchParams();
  suspend = false;
  createSession.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }))
  );
});
afterEach(() => vi.unstubAllGlobals());

const field = (label: string) => screen.getByLabelText(label);

describe("New-run form accessibility", () => {
  it("marks the required fields aria-required (the trend only for creative runs)", () => {
    render(<Home />);
    for (const label of ["Brand name", "Target product", "Target audience", "Key selling points"]) {
      expect(field(label)).toHaveAttribute("aria-required", "true");
    }
    expect(screen.queryByLabelText("Target search trend")).toBeNull();
    fireEvent.click(screen.getAllByRole("radio")[1]); // Creative run
    expect(field("Target search trend")).toHaveAttribute("aria-required", "true");
  });

  it("flags and describes an empty required field once it is left, and clears it when filled", () => {
    render(<Home />);
    const brand = field("Brand name");
    expect(brand).not.toHaveAttribute("aria-invalid");
    fireEvent.blur(brand);
    expect(brand).toHaveAttribute("aria-invalid", "true");
    expect(brand).toHaveAccessibleDescription("Enter the brand name.");

    fireEvent.change(brand, { target: { value: "PRS" } });
    expect(brand).not.toHaveAttribute("aria-invalid");
    expect(screen.queryByText("Enter the brand name.")).toBeNull();
  });

  it("ties the disabled-submit hint to the button, in a polite live region", () => {
    render(<Home />);
    const submit = screen.getByRole("button", { name: submitLabel("trend_scout") });
    expect(submit).toBeDisabled();
    expect(submit).toHaveAccessibleDescription("Fill in the campaign fields to start.");
    expect(screen.getByText("Fill in the campaign fields to start.")).toHaveAttribute("aria-live", "polite");
  });

  it("on a blocked Ctrl/⌘+Enter, flags every missing field and focuses the first", () => {
    render(<Home />);
    fireEvent.change(field("Brand name"), { target: { value: "PRS" } });
    fireEvent.keyDown(field("Brand name"), { key: "Enter", ctrlKey: true });
    expect(createSession).not.toHaveBeenCalled();
    expect(field("Brand name")).not.toHaveAttribute("aria-invalid");
    for (const label of ["Target product", "Target audience", "Key selling points"]) {
      expect(field(label)).toHaveAttribute("aria-invalid", "true");
    }
    expect(field("Target product")).toHaveFocus();
  });

  it("shows the key hint as key caps once the form is valid", () => {
    search = new URLSearchParams({
      brand: "PRS",
      targetAudience: "guitarists",
      targetProduct: "SE CE24",
      keySellingPoints: "tone",
      agent: "trend_scout",
    });
    render(<Home />);
    const keys = document.querySelectorAll('[data-slot="kbd"]');
    expect([...keys].map((k) => k.textContent)).toEqual(["Ctrl", "⌘", "Enter"]);
  });

  it("renders a layout skeleton, not a blank page, while the search params suspend", () => {
    suspend = true;
    render(<Home />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Loading the new-run form…");
    expect(status.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(3);
  });
});
