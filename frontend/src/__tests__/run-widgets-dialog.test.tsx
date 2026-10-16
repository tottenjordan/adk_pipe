// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PipelineWidget } from "@/app/run/[sessionId]/run-widgets";

const copies = [
  { headline: "First headline", body_text: "Body one" },
  { headline: "Second headline", body_text: "Body two" },
];

function renderWidget() {
  render(
    <>
      <button type="button">Outside</button>
      <PipelineWidget label="Ad copies" stateKey="ad_copies" noun={["ad copy", "ad copies"]} data={{ ad_copies: copies }} />
    </>
  );
  return screen.getByRole("button", { name: /2 ad copies/ });
}

describe("PipelineWidget dialog", () => {
  it("opens a labelled modal dialog with focus moved inside it", async () => {
    const trigger = renderWidget();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Ad copies: 2 ad copies" });
    expect(dialog).toHaveTextContent("First headline");
    expect(dialog).toHaveTextContent("Second headline");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    // Modal: the rest of the page is hidden from assistive tech while it is open.
    expect(screen.queryByRole("button", { name: "Outside" })).not.toBeInTheDocument();
  });

  it("closes on Escape and returns focus to the open button", async () => {
    const trigger = renderWidget();
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await act(async () => {
      fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("closes from the close button and returns focus", async () => {
    const trigger = renderWidget();
    trigger.focus();
    fireEvent.click(trigger);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
