// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pathname = vi.hoisted(() => ({ current: "/" as string | null }));

vi.mock("next/navigation", () => ({
  usePathname: () => pathname.current,
}));

import { SiteHeader } from "@/components/site-header";

describe("SiteHeader", () => {
  beforeEach(() => {
    pathname.current = "/";
  });

  it("renders the small banner with intrinsic dimensions so it never shifts layout", () => {
    const { container } = render(<SiteHeader />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img).toHaveAttribute("src", "/trend_trawler_banner.png");
    expect(img).toHaveAttribute("width", "54");
    expect(img).toHaveAttribute("height", "36");
    expect(img).toHaveAttribute("alt", "");
  });

  it("renders nothing on a public share page", () => {
    pathname.current = "/s/abcdefghijklmnop1234";
    const { container } = render(<SiteHeader />);
    expect(container).toBeEmptyDOMElement();
  });
});
