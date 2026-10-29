// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchReport } from "@/components/research-report";

describe("ResearchReport enterprise citations", () => {
  it("renders distinct Enterprise badges for ent-N (HTTP and gs://) and [Enterprise: ...] without gs:// hrefs", () => {
    const sources = {
      "src-1": { title: "usatoday.com", url: "https://usatoday.example/story" },
    };
    const enterpriseSources = {
      "ent-1": {
        title: "Brand Portal Spec",
        url: "https://portal.example/spec",
        source_type: "enterprise",
      },
      "ent-2": {
        title: "Internal Claims PDF",
        url: "gs://brand-kb/claims.pdf",
        source_type: "enterprise",
      },
    };
    const markdown = [
      "# Research Summary",
      "",
      'Public trend <cite source="src-1" />, internal portal <cite source="ent-1" />,',
      'vault doc <cite source="ent-2" />, and rendered [Enterprise: Tone Policy].',
    ].join("\n");

    const { container } = render(
      <ResearchReport
        markdown={markdown}
        sources={sources}
        enterpriseSources={enterpriseSources}
      />
    );

    const chips = container.querySelectorAll('[data-enterprise-citation="true"]');
    expect(chips.length).toBe(3);

    // Zero broken gs:// links anywhere in the rendered DOM.
    const anchors = Array.from(container.querySelectorAll("a"));
    for (const a of anchors) {
      expect(a.getAttribute("href") ?? "").not.toMatch(/^gs:\/\//i);
    }

    expect(screen.getAllByText("Internal Claims PDF").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Tone Policy")).toBeInTheDocument();

    const sourcesSection = screen.getByRole("region", { name: "Sources" });
    expect(within(sourcesSection).getAllByText("Enterprise")).toHaveLength(2);
    expect(within(sourcesSection).getByText("Brand Portal Spec")).toHaveAttribute(
      "href",
      "https://portal.example/spec"
    );
  });

  it("prevents markdown link injection, preserves GFM-styled titles, and drops javascript: URLs", () => {
    const sources = {
      "src-1": { title: "Evil JS", url: "javascript:alert(1)" },
    };
    const enterpriseSources = {
      "ent-1": {
        title: "Spec](https://evil.example) [Draft]",
        url: "gs://brand-kb/spec.pdf",
        source_type: "enterprise",
      },
      "ent-2": {
        title: "Brand *Core* _2026_ Guide",
        url: "gs://brand-kb/core.pdf",
        source_type: "enterprise",
      },
    };
    const markdown =
      'Unsafe <cite source="src-1" />, adversarial <cite source="ent-1" />, ' +
      'styled <cite source="ent-2" />, and inline [Enterprise: Tone *Core* _2026_ Policy].';

    const { container } = render(
      <ResearchReport
        markdown={markdown}
        sources={sources}
        enterpriseSources={enterpriseSources}
      />
    );

    const anchors = Array.from(container.querySelectorAll("a"));
    for (const a of anchors) {
      const href = a.getAttribute("href") ?? "";
      expect(href).not.toContain("evil.example");
      expect(href.toLowerCase()).not.toContain("javascript:");
    }

    const chips = Array.from(
      container.querySelectorAll('[data-enterprise-citation="true"]')
    );
    expect(chips).toHaveLength(3);
    expect(chips[0].textContent).toContain("Spec](https://evil.example) [Draft]");
    expect(chips[1].textContent).toContain("Brand *Core* _2026_ Guide");
    expect(chips[2].textContent).toContain("Tone Core 2026 Policy");
  });
});
