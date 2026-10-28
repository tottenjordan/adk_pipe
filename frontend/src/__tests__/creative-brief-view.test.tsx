// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CreativeBrief, CreativeBriefOutput } from "@/components/creative-brief";
import { parseCreativeBrief } from "@/lib/creative-brief";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

const brief = parseCreativeBrief(SAMPLE_CREATIVE_BRIEF)!;
const sources = { "src-2": { title: "usatoday.com", domain: "usatoday.com" } };

describe("CreativeBrief", () => {
  it("leads with the proposition, then the insight", () => {
    const { container } = render(<CreativeBrief brief={brief} sources={sources} />);
    const text = container.textContent ?? "";
    const smp = text.indexOf(brief.singleMindedProposition);
    expect(smp).toBeGreaterThanOrEqual(0);
    expect(text.indexOf(brief.insight)).toBeGreaterThan(smp);
    expect(text.indexOf(brief.objective)).toBeGreaterThan(text.indexOf(brief.insight));
  });

  it("shows the trend fit as n/5 with the mode label, motifs and risks", () => {
    render(<CreativeBrief brief={brief} />);
    expect(screen.getByText("3/5")).toBeInTheDocument();
    expect(screen.getByText("Cultural fit")).toBeInTheDocument();
    expect(screen.getByText("lottery ball draw")).toBeInTheDocument();
    expect(screen.getByText("gambling glamorisation")).toBeInTheDocument();
  });

  it("names cited sources in mono and the brief as a plain source", () => {
    render(<CreativeBrief brief={brief} sources={sources} />);
    const id = screen.getByText("src-2");
    expect(id).toHaveClass("font-mono");
    expect(screen.getByText("usatoday.com")).toBeInTheDocument();
    expect(screen.getByText("From the brief")).toBeInTheDocument();
  });

  it("renders just the id for a src-N with no matching source", () => {
    render(<CreativeBrief brief={brief} sources={{}} />);
    const id = screen.getByText("src-2");
    expect(id).toHaveClass("font-mono");
    expect(id.parentElement).toHaveTextContent(/^src-2$/);
    expect(screen.queryByText("usatoday.com")).not.toBeInTheDocument();
  });

  it("lists brand cues, must-include, keep-out and each angle", () => {
    render(<CreativeBrief brief={brief} />);
    expect(screen.getByText("Tone: warm, witty, never smug")).toBeInTheDocument();
    expect(screen.getByText("mock other guitar brands")).toBeInTheDocument();
    expect(screen.getByText("85/15 S pickups")).toBeInTheDocument();
    expect(screen.getByText("real lottery winners' likenesses")).toBeInTheDocument();
    const angles = screen.getByText("Creative angles").parentElement!;
    expect(within(angles).getAllByRole("listitem")).toHaveLength(3);
    expect(within(angles).getByText("Jackpot rig")).toBeInTheDocument();
  });

  it("omits empty sections for a minimal brief", () => {
    render(<CreativeBrief brief={parseCreativeBrief({ single_minded_proposition: "One idea." })!} />);
    expect(screen.getByText("One idea.")).toBeInTheDocument();
    for (const label of ["Insight", "Trend fit", "Reasons to believe", "Brand", "Creative angles"]) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it("renders an Enterprise badge, mono ent-N id, and resolved title for enterprise RTBs", () => {
    const entBrief = parseCreativeBrief({
      ...SAMPLE_CREATIVE_BRIEF,
      reasons_to_believe: [
        ...SAMPLE_CREATIVE_BRIEF.reasons_to_believe,
        {
          claim: "Approved internal durability testing meets 10,000-cycle spec.",
          source_id: "ent-1",
        },
      ],
    })!;
    const enterpriseSources = {
      "ent-1": {
        title: "PRS Brand Guidelines Q4",
        url: "gs://brand-kb/prs-guidelines.pdf",
        source_type: "enterprise",
      },
    };
    const { container } = render(
      <CreativeBrief brief={entBrief} sources={sources} enterpriseSources={enterpriseSources} />
    );
    const badge = container.querySelector('[data-enterprise-citation="true"]');
    expect(badge).not.toBeNull();
    expect(badge).toHaveTextContent("Enterprise");
    const entId = screen.getByText("ent-1");
    expect(entId).toHaveClass("font-mono");
    expect(screen.getByText("PRS Brand Guidelines Q4")).toBeInTheDocument();
  });

  it("normalizes ENT_01 and [ent-1] spelling variants when resolving enterpriseSources", () => {
    const variantBrief = parseCreativeBrief({
      single_minded_proposition: "Precision tone.",
      reasons_to_believe: [{ claim: "Verified spec.", source_id: "ENT_01" }],
    })!;
    const { container } = render(
      <CreativeBrief
        brief={variantBrief}
        enterpriseSources={{
          "ent-1": { title: "PRS Brand Guidelines Q4", source_type: "enterprise" },
        }}
      />
    );
    expect(container.querySelector('[data-enterprise-citation="true"]')).toHaveTextContent(
      "Enterprise"
    );
    expect(screen.getByText("PRS Brand Guidelines Q4")).toBeInTheDocument();
  });
});

describe("CreativeBriefOutput", () => {
  it("shows the proposition and expands to the full brief, then collapses", async () => {
    render(<CreativeBriefOutput brief={brief} />);
    expect(screen.getByText(brief.singleMindedProposition)).toBeInTheDocument();
    expect(screen.queryByText("Trend fit")).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /show the full brief/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAccessibleName(/hide the full brief/i);
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!);
    expect(panel).not.toBeNull();
    expect(panel).toBeVisible();
    expect(within(panel!).getByText("Trend fit")).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(screen.queryByText("Trend fit")).not.toBeInTheDocument());
  });

  it("forwards enterpriseSources into the expanded brief", () => {
    const entBrief = parseCreativeBrief({
      single_minded_proposition: "Precision tone.",
      reasons_to_believe: [{ claim: "Verified spec sheet.", source_id: "ent-2" }],
    })!;
    render(
      <CreativeBriefOutput
        brief={entBrief}
        enterpriseSources={{ "ent-2": { title: "SE CE24 Spec Sheet", source_type: "enterprise" } }}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /show the full brief/i }));
    expect(screen.getByText("Enterprise")).toBeInTheDocument();
    expect(screen.getByText("ent-2")).toBeInTheDocument();
    expect(screen.getByText("SE CE24 Spec Sheet")).toBeInTheDocument();
  });
});
