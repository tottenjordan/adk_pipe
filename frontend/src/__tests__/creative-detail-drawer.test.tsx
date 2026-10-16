// @vitest-environment jsdom
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreativeDetailDrawer } from "@/app/experiments/[experimentId]/creative-detail";
import type { Lane } from "@/lib/scoreboard";

const lane: Lane = {
  creativeId: "c1",
  arm: null,
  rank: 1,
  name: "Jackpot rig",
  headline: "Win the room",
  color: "#0077A8",
  finalShare: null,
  share: [],
  impressions: null,
  clicks: null,
  clickRate: null,
  trueCtr: null,
  segmentsWon: [],
};

function renderDrawer(open = true) {
  const onClose = vi.fn();
  render(<CreativeDetailDrawer lane={open ? lane : null} lanes={[lane]} series={null} explain={false} onClose={onClose} />);
  return onClose;
}

describe("CreativeDetailDrawer", () => {
  it("is a labelled dialog that closes on Escape", async () => {
    const onClose = renderDrawer();
    const dialog = await screen.findByRole("dialog", { name: /Jackpot rig/ });
    await act(async () => {
      fireEvent.keyDown(dialog, { key: "Escape" });
    });
    expect(onClose).toHaveBeenCalled();
  });

  it("slides in from the right on the drawer curve, a plain fade under reduced motion", async () => {
    renderDrawer();
    const dialog = await screen.findByRole("dialog");
    for (const c of [
      "duration-(--dur-drawer)",
      "ease-(--ease-drawer)",
      "motion-safe:data-starting-style:translate-x-full",
      "motion-safe:data-ending-style:translate-x-full",
      "data-ending-style:duration-[calc(var(--dur-drawer)*0.75)]",
      "motion-reduce:data-starting-style:opacity-0",
    ]) {
      expect(dialog).toHaveClass(c);
    }
  });
});
