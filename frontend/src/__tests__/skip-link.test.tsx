// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { SkipLink } from "@/components/skip-link";

vi.mock("next/font/google", () => ({
  Archivo: () => ({ variable: "font-archivo" }),
  JetBrains_Mono: () => ({ variable: "font-mono" }),
}));
vi.mock("@/components/site-header", () => ({ SiteHeader: () => <header>header</header> }));
vi.mock("@/components/shortcut-help", () => ({ ShortcutHelp: () => <footer>shortcuts</footer> }));
vi.mock("@/app/globals.css", () => ({}));

describe("SkipLink", () => {
  it("jumps to #main and is visually hidden until focused", () => {
    render(<SkipLink />);
    const link = screen.getByRole("link", { name: "Skip to content" });
    expect(link.getAttribute("href")).toBe("#main");
    expect(link.className).toContain("sr-only");
    expect(link.className).toContain("focus:not-sr-only");
  });
});

describe("RootLayout", () => {
  it("renders the skip link before the header and a focusable #main", async () => {
    const { default: RootLayout } = await import("@/app/layout");
    const html = renderToStaticMarkup(<RootLayout><p>page</p></RootLayout>);
    const doc = new DOMParser().parseFromString(html, "text/html");
    const body = doc.body;
    expect(body.firstElementChild?.getAttribute("href")).toBe("#main");
    expect(body.children[1]?.tagName).toBe("HEADER");
    const main = doc.getElementById("main");
    expect(main?.tagName).toBe("MAIN");
    expect(main?.getAttribute("tabindex")).toBe("-1");
    expect(main?.textContent).toBe("page");
    // the shortcut help (and its footer link) is mounted once, after the content
    expect(main?.nextElementSibling?.tagName).toBe("FOOTER");
  });
});
