// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { QuietDisclosure } from "@/components/quiet-disclosure";
import { OverridesDisclosure } from "@/components/scenario-overrides";
import { ResultsHeader } from "@/app/results/[sessionId]/results-header";

/** Open, check the panel, close, check it is gone: the shared disclosure contract. */
async function expectToggles(trigger: HTMLElement, content: string) {
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText(content)).not.toBeInTheDocument();
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  const panel = document.getElementById(trigger.getAttribute("aria-controls")!)!;
  expect(panel).toBeVisible();
  expect(within(panel).getByText(content)).toBeInTheDocument();
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  await waitFor(() => expect(screen.queryByText(content)).not.toBeInTheDocument());
  return panel;
}

describe("QuietDisclosure", () => {
  it("toggles aria-expanded and the panel", async () => {
    render(
      <QuietDisclosure title="Session state" count={3}>
        <p>raw state</p>
      </QuietDisclosure>
    );
    await expectToggles(screen.getByRole("button", { name: /Session state/ }), "raw state");
  });

  it("animates the panel by height and turns the chevron on the same timing", () => {
    render(
      <QuietDisclosure title="Artifacts">
        <p>files</p>
      </QuietDisclosure>
    );
    const trigger = screen.getByRole("button", { name: /Artifacts/ });
    fireEvent.click(trigger);
    const panel = document.getElementById(trigger.getAttribute("aria-controls")!)!;
    for (const c of ["h-(--collapsible-panel-height)", "data-starting-style:h-0", "data-ending-style:h-0", "duration-(--dur-dialog)"]) {
      expect(panel).toHaveClass(c);
    }
    expect(trigger).toHaveAttribute("data-panel-open");
    expect(trigger).toHaveClass("group/disclosure");
    const chevron = trigger.querySelector("svg")!;
    expect(chevron).toHaveClass("group-data-[panel-open]/disclosure:rotate-180");
    expect(chevron).toHaveClass("group-data-[panel-open]/disclosure:duration-(--dur-dialog)");
    expect(chevron).toHaveClass("motion-reduce:transition-none");
  });
});

describe("OverridesDisclosure", () => {
  it("toggles the tuned settings, with the help tip outside the toggle", async () => {
    render(<OverridesDisclosure scenario="clear_winner" overrides={{ gapScale: 2 }} />);
    const trigger = screen.getByRole("button", { name: /Tuned reader settings/ });
    expect(within(trigger).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "About tuned reader settings" })).toBeInTheDocument();
    await expectToggles(trigger, "Gap between creatives");
  });

  it("starts open with defaultOpen", () => {
    render(<OverridesDisclosure scenario="clear_winner" overrides={{ gapScale: 2 }} defaultOpen />);
    expect(screen.getByRole("button", { name: /Tuned reader settings/ })).toHaveAttribute("aria-expanded", "true");
  });
});

describe("ResultsHeader campaign details", () => {
  it("toggles the details panel and its label", async () => {
    render(
      <ResultsHeader
        appName="creative_agent"
        sessionId="s1"
        summary="Acme x Trend"
        campaignFields={[{ key: "brand", label: "Brand", value: "Acme Guitars" }]}
        visualDirectionFields={[]}
        gcsUri=""
        galleryUrl={null}
      />
    );
    const trigger = screen.getByRole("button", { name: /Campaign details/ });
    fireEvent.click(trigger);
    expect(trigger).toHaveAccessibleName(/Hide details/);
    fireEvent.click(trigger);
    expect(trigger).toHaveAccessibleName(/Campaign details/);
    await expectToggles(trigger, "Acme Guitars");
  });
});
