// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AddShiftMenu } from "@/app/experiments/[experimentId]/add-shift-menu";
import { KIND_LABELS, SHIFT_KINDS, SHIFT_PRESETS } from "@/lib/shifts";

function setup(disabled = false) {
  const onAdd = vi.fn();
  render(<AddShiftMenu onAdd={onAdd} disabled={disabled} />);
  return { onAdd, trigger: screen.getByRole("button", { name: /add shift/i }) };
}

async function openWithKeyboard(trigger: HTMLElement) {
  act(() => trigger.focus());
  await act(async () => {
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
  });
  return screen.findByRole("menu");
}

describe("AddShiftMenu", () => {
  it("is a real menu button, closed by default", () => {
    const { trigger } = setup();
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("lists every kind and preset as menu items, kinds first", async () => {
    const { trigger } = setup();
    await openWithKeyboard(trigger);
    const items = screen.getAllByRole("menuitem");
    expect(items).toHaveLength(SHIFT_KINDS.length + SHIFT_PRESETS.length);
    expect(items[0]).toHaveTextContent(KIND_LABELS[SHIFT_KINDS[0]]);
  });

  it("moves focus with the arrow keys and picks with Enter", async () => {
    const { trigger, onAdd } = setup();
    await openWithKeyboard(trigger);
    const items = screen.getAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    await act(async () => {
      fireEvent.keyDown(items[0], { key: "ArrowDown" });
    });
    expect(document.activeElement).toBe(items[1]);
    await act(async () => {
      fireEvent.keyDown(items[1], { key: "Enter" });
    });
    expect(onAdd).toHaveBeenCalledWith(SHIFT_KINDS[1], null);
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("adds a preset by id", async () => {
    const { trigger, onAdd } = setup();
    await openWithKeyboard(trigger);
    fireEvent.click(screen.getByRole("menuitem", { name: new RegExp(SHIFT_PRESETS[0].label) }));
    expect(onAdd).toHaveBeenCalledWith(null, SHIFT_PRESETS[0].id);
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    const { trigger } = setup();
    await openWithKeyboard(trigger);
    await act(async () => {
      fireEvent.keyDown(document.activeElement ?? trigger, { key: "Escape" });
    });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(trigger);
  });

  it("cannot open while disabled", () => {
    const { trigger } = setup(true);
    expect(trigger).toBeDisabled();
  });
});
