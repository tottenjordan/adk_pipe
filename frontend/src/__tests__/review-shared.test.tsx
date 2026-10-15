// @vitest-environment jsdom
import { fireEvent, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useApproveShortcut } from "@/app/run/[sessionId]/review-shared";

const ctrlEnter = (init: KeyboardEventInit = {}) =>
  fireEvent.keyDown(document, { key: "Enter", ctrlKey: true, ...init });

describe("useApproveShortcut", () => {
  it("approves on Ctrl+Enter and Cmd+Enter, not on a bare Enter", () => {
    const onApprove = vi.fn();
    renderHook(() => useApproveShortcut(onApprove));
    fireEvent.keyDown(document, { key: "Enter" });
    expect(onApprove).not.toHaveBeenCalled();
    ctrlEnter();
    fireEvent.keyDown(document, { key: "Enter", metaKey: true });
    expect(onApprove).toHaveBeenCalledTimes(2);
  });

  it("calls the latest onApprove after a re-render", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ fn }) => useApproveShortcut(fn), {
      initialProps: { fn: first },
    });
    rerender({ fn: second });
    ctrlEnter();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("skips key repeats, taken events, a disabled shortcut and an unmounted panel", () => {
    const onApprove = vi.fn();
    const { rerender, unmount } = renderHook(({ enabled }) => useApproveShortcut(onApprove, enabled), {
      initialProps: { enabled: true },
    });
    ctrlEnter({ repeat: true });
    const taken = new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, cancelable: true });
    taken.preventDefault();
    document.dispatchEvent(taken);
    rerender({ enabled: false });
    ctrlEnter();
    expect(onApprove).not.toHaveBeenCalled();
    rerender({ enabled: true });
    ctrlEnter();
    expect(onApprove).toHaveBeenCalledTimes(1);
    unmount();
    ctrlEnter();
    expect(onApprove).toHaveBeenCalledTimes(1);
  });
});
