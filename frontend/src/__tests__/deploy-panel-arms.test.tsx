// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { buildProofs, type AdCopy, type VisualConcept } from "@/lib/eval-matching";
import { DeployPanel } from "@/app/results/[sessionId]/deploy-panel";

const VALID_UNTIL = "2026-10-14T23:59:59-04:00";
const AT = Date.parse(VALID_UNTIL);
const situation = { phase: "upcoming", timely_ok: true, valid_until: VALID_UNTIL };

const concepts = [
  { concept_name: "Countdown", headline: "Game day", ad_copy_id: 1 },
  { concept_name: "Evergreen", headline: "Always on", ad_copy_id: 2 },
  { concept_name: "Classic", headline: "Classic", ad_copy_id: 3 },
] as VisualConcept[];
// The timely flag sits on the paired copy only.
const copies = [
  { original_id: 1, headline: "Game day", uses_trend_state: true },
  { original_id: 2, headline: "Always on" },
  { original_id: 3, headline: "Classic" },
] as AdCopy[];

function renderPanel(now: number) {
  return render(
    <DeployPanel
      proofs={buildProofs(concepts, copies, null, undefined, undefined, situation, now)}
      appName="creative_agent"
      sessionId="s1"
      imageUrlFor={() => null}
      stoppedEarly={false}
      now={now}
    />,
  );
}

const checkbox = (i: number) => document.getElementById(`deploy-creative-${i}`) as HTMLInputElement;

const WARNING = "Its moment ended on Oct 14, 23:59; fine for a simulation, but don't use it live.";

describe("deploy panel timely expiry", () => {
  it("shows no warning before the moment ends", () => {
    renderPanel(AT - 1000);
    expect(checkbox(0).disabled).toBe(false);
    expect(checkbox(0)).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText(WARNING)).toBeNull();
  });

  it("keeps an expired timely creative selectable with a described warning", () => {
    renderPanel(AT);
    expect(checkbox(0).disabled).toBe(false);
    expect(checkbox(0)).toHaveAttribute("aria-describedby", "deploy-creative-0-warning");
    expect(document.getElementById("deploy-creative-0-warning")).toHaveTextContent(WARNING);
    expect(checkbox(1)).not.toHaveAttribute("aria-describedby");
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect([0, 1, 2].map((i) => checkbox(i).checked)).toEqual([true, true, true]);
  });

  it("keeps a pick whose moment ends while the page is open, now warned", () => {
    const { rerender } = renderPanel(AT - 1000);
    fireEvent.click(checkbox(0));
    fireEvent.click(checkbox(1));
    expect(screen.getByText(/2 of 3/)).toBeInTheDocument();
    rerender(
      <DeployPanel
        proofs={buildProofs(concepts, copies, null, undefined, undefined, situation, AT)}
        appName="creative_agent"
        sessionId="s1"
        imageUrlFor={() => null}
        stoppedEarly={false}
        now={AT}
      />,
    );
    expect(checkbox(0).checked).toBe(true);
    expect(screen.getByText(/2 of 3/)).toBeInTheDocument();
    expect(screen.getByText(WARNING)).toBeInTheDocument();
  });
});

describe("deploy panel cast creatives", () => {
  it("lets a cast creative be picked (the api checks its consent)", () => {
    const images = { Countdown: { gcs_uri: "gs://b/c.png", cast: true, consent_id: "consentAAAA1" } };
    render(
      <DeployPanel
        proofs={buildProofs(concepts, copies, null, images)}
        appName="creative_agent"
        sessionId="s1"
        imageUrlFor={() => null}
        stoppedEarly={false}
        now={AT - 1000}
      />,
    );
    expect(checkbox(0).disabled).toBe(false);
    expect(checkbox(0)).not.toHaveAttribute("aria-describedby");
    fireEvent.click(checkbox(0));
    expect(checkbox(0).checked).toBe(true);
  });
});
