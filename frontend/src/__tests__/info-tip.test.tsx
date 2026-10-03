import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InfoTip } from "@/components/ui/info-tip";

const HELP = "Regret is the expected clicks lost.";

function setup() {
  render(<InfoTip label="About cumulative regret">{HELP}</InfoTip>);
  return screen.getByRole("button", { name: "About cumulative regret" });
}

describe("InfoTip", () => {
  it("renders a real button trigger with an aria-label and hidden content", () => {
    const trigger = setup();
    expect(trigger.tagName).toBe("BUTTON");
    expect(trigger).toHaveAttribute("type", "button");
    expect(screen.queryByText(HELP)).not.toBeInTheDocument();
  });

  it("opens on keyboard focus and describes the trigger", async () => {
    const trigger = setup();
    act(() => trigger.focus());
    const content = await screen.findByText(HELP);
    expect(trigger).toHaveAttribute("aria-describedby", content.closest("[id]")?.id);
  });

  it("opens on click (touch/mouse press)", async () => {
    const trigger = setup();
    fireEvent.pointerDown(trigger);
    fireEvent.focus(trigger);
    fireEvent.click(trigger);
    expect(await screen.findByText(HELP)).toBeInTheDocument();
  });

  it("closes on Escape", async () => {
    const trigger = setup();
    act(() => trigger.focus());
    await screen.findByText(HELP);
    fireEvent.keyDown(document.activeElement ?? trigger, { key: "Escape" });
    await waitFor(() => expect(screen.queryByText(HELP)).not.toBeInTheDocument());
  });

  it("closes on blur", async () => {
    const trigger = setup();
    act(() => trigger.focus());
    await screen.findByText(HELP);
    act(() => trigger.blur());
    await waitFor(() => expect(screen.queryByText(HELP)).not.toBeInTheDocument());
  });
});
