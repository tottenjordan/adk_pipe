// @vitest-environment jsdom
import { fireEvent, render, screen, within } from "@testing-library/react";
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
});

describe("CreativeBriefOutput", () => {
  it("shows the proposition and expands to the full brief", () => {
    render(<CreativeBriefOutput brief={brief} />);
    expect(screen.getByText(brief.singleMindedProposition)).toBeInTheDocument();
    expect(screen.queryByText("Trend fit")).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /show the full brief/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const panel = document.getElementById(toggle.getAttribute("aria-controls")!);
    expect(panel).not.toBeNull();
    expect(panel).not.toBeVisible();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Trend fit")).toBeInTheDocument();
    expect(panel).toBeVisible();
  });
});
