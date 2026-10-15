// @vitest-environment jsdom
import { StrictMode } from "react";
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearReviewDrafts, useReviewDraft } from "@/app/run/[sessionId]/review-drafts";

afterEach(() => clearReviewDrafts());

const useList = (key: string | undefined, init = () => ["a"]) => useReviewDraft<string[]>(key, init);

describe("useReviewDraft", () => {
  it("chains functional updates and restores the result after a remount", () => {
    const first = renderHook(() => useList("fc-1:list"));
    act(() => {
      const set = first.result.current[1];
      set((prev) => [...prev, "b"]);
      set((prev) => [...prev, "c"]);
      set((prev) => prev.filter((v) => v !== "a"));
    });
    expect(first.result.current[0]).toEqual(["b", "c"]);
    first.unmount();
    const init = vi.fn(() => ["fresh"]);
    const second = renderHook(() => useList("fc-1:list", init));
    expect(second.result.current[0]).toEqual(["b", "c"]);
    expect(init).not.toHaveBeenCalled();
  });

  it("mixes plain and functional updates across renders", () => {
    const { result, unmount } = renderHook(() => useList("fc-1:list"));
    act(() => result.current[1](["x"]));
    act(() => result.current[1]((prev) => [...prev, "y"]));
    expect(result.current[0]).toEqual(["x", "y"]);
    unmount();
    expect(renderHook(() => useList("fc-1:list")).result.current[0]).toEqual(["x", "y"]);
  });

  it("applies a functional update once under StrictMode", () => {
    const updater = vi.fn((prev: string[]) => [...prev, "b"]);
    const { result, unmount } = renderHook(() => useList("fc-1:list"), { wrapper: StrictMode });
    act(() => result.current[1](updater));
    expect(result.current[0]).toEqual(["a", "b"]);
    expect(updater).toHaveBeenCalledTimes(1);
    unmount();
    expect(renderHook(() => useList("fc-1:list")).result.current[0]).toEqual(["a", "b"]);
  });

  it("keeps no draft without a key", () => {
    const first = renderHook(() => useList(undefined));
    act(() => first.result.current[1]((prev) => [...prev, "b"]));
    expect(first.result.current[0]).toEqual(["a", "b"]);
    first.unmount();
    expect(renderHook(() => useList(undefined)).result.current[0]).toEqual(["a"]);
  });
});
