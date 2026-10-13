// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { buildProofs, type AdCopy, type VisualConcept } from "@/lib/eval-matching";
import { EXPIRED_NOT_DEPLOYABLE } from "@/lib/experiments";
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

describe("deploy panel timely expiry", () => {
  it("keeps a timely creative selectable before its moment ends", () => {
    renderPanel(AT - 1000);
    expect(checkbox(0).disabled).toBe(false);
    expect(screen.queryByText(EXPIRED_NOT_DEPLOYABLE)).toBeNull();
  });

  it("disables an expired timely creative with the reason", () => {
    renderPanel(AT);
    expect(checkbox(0).disabled).toBe(true);
    expect(checkbox(0)).toHaveAttribute("aria-describedby", "deploy-creative-0-excluded");
    expect(document.getElementById("deploy-creative-0-excluded")).toHaveTextContent(EXPIRED_NOT_DEPLOYABLE);
    expect(checkbox(1).disabled).toBe(false);
    // "Select all" skips it
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(checkbox(0).checked).toBe(false);
    expect(checkbox(1).checked).toBe(true);
    expect(checkbox(2).checked).toBe(true);
  });

  it("drops a pick that expires while the page is open", () => {
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
    expect(checkbox(0).checked).toBe(false);
    expect(screen.getByText(/1 of 3/)).toBeInTheDocument();
  });
});
