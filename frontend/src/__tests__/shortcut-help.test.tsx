// @vitest-environment jsdom
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const pathname = vi.hoisted(() => ({ current: "/" as string | null }));
vi.mock("next/navigation", () => ({ usePathname: () => pathname.current }));

import { ShortcutHelp } from "@/components/shortcut-help";
import { ShortcutHint } from "@/app/run/[sessionId]/review-shared";
import {
  SCOPE_LABELS,
  SHORTCUTS,
  hasCommandModifier,
  isTypingTarget,
  shortcutsByScope,
} from "@/lib/shortcuts";

const question = (target: Element | Document = document, init: KeyboardEventInit = {}) =>
  fireEvent.keyDown(target, { key: "?", shiftKey: true, ...init });

beforeEach(() => {
  pathname.current = "/";
});

describe("shortcut registry", () => {
  it("groups every shortcut once, in scope order", () => {
    const groups = shortcutsByScope();
    expect(groups.map((g) => g.title)).toEqual(Object.values(SCOPE_LABELS));
    const listed = groups.flatMap((g) => g.items);
    expect(listed).toHaveLength(Object.keys(SHORTCUTS).length);
    expect(new Set(listed.map((s) => s.label)).size).toBe(listed.length);
  });

  it("treats text fields and key-handling widgets as typing targets", () => {
    document.body.innerHTML = `
      <input id="i" /><textarea id="t"></textarea><select id="s"></select>
      <div contenteditable="true"><span id="ce">x</span></div>
      <div contenteditable="false" id="nce">x</div>
      <button id="b">b</button><div role="combobox" id="cb"></div>`;
    const el = (id: string) => document.getElementById(id);
    for (const id of ["i", "t", "s", "ce", "cb"]) expect(isTypingTarget(el(id))).toBe(true);
    for (const id of ["nce", "b"]) expect(isTypingTarget(el(id))).toBe(false);
    expect(isTypingTarget(document.body)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
    document.body.innerHTML = "";
  });

  it("counts Ctrl, ⌘ and Alt as command modifiers, not Shift", () => {
    const none = { ctrlKey: false, metaKey: false, altKey: false };
    expect(hasCommandModifier(none)).toBe(false);
    expect(hasCommandModifier({ ...none, ctrlKey: true })).toBe(true);
    expect(hasCommandModifier({ ...none, metaKey: true })).toBe(true);
    expect(hasCommandModifier({ ...none, altKey: true })).toBe(true);
  });
});

describe("ShortcutHelp", () => {
  it("opens on ? and lists every registered shortcut by scope", async () => {
    render(<ShortcutHelp />);
    expect(screen.queryByRole("dialog")).toBeNull();
    question();
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    for (const title of Object.values(SCOPE_LABELS)) {
      expect(within(dialog).getByRole("heading", { name: title })).toBeInTheDocument();
    }
    for (const s of Object.values(SHORTCUTS)) {
      expect(within(dialog).getByText(s.label)).toBeInTheDocument();
    }
    // the score range collapses to 1–5; Ctrl/⌘+Enter lists both combos
    const score = within(dialog).getByText(SHORTCUTS.score.label).parentElement!;
    expect(score).toHaveTextContent("1–5");
    const approve = within(dialog).getByText(SHORTCUTS.approve.label).parentElement!;
    expect(approve).toHaveTextContent(/Ctrl\s*Enter\s*or\s*⌘\s*Enter/);
  });

  it("ignores ? typed in an input, a textarea or a contenteditable", () => {
    render(
      <div>
        <input aria-label="field" />
        <textarea aria-label="area" />
        <div contentEditable suppressContentEditableWarning data-testid="ce">
          x
        </div>
        <ShortcutHelp />
      </div>
    );
    question(screen.getByLabelText("field"));
    question(screen.getByLabelText("area"));
    question(screen.getByTestId("ce"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ignores ? with Ctrl, ⌘ or Alt held", () => {
    render(<ShortcutHelp />);
    for (const mod of ["ctrlKey", "metaKey", "altKey"]) question(document, { [mod]: true });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens from the footer button, which advertises the key", async () => {
    render(<ShortcutHelp />);
    const button = screen.getByRole("button", { name: /Keyboard shortcuts/ });
    expect(button).toHaveAttribute("aria-keyshortcuts", SHORTCUTS.help.aria);
    fireEvent.click(button);
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  });

  it("renders nothing and listens for nothing on a public share page", () => {
    pathname.current = "/s/abcdefghijklmnop1234";
    const { container } = render(<ShortcutHelp />);
    expect(container).toBeEmptyDOMElement();
    question();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("ShortcutHint", () => {
  it("renders the approve combos from the registry", () => {
    const { container } = render(<ShortcutHint action="confirm" />);
    expect(container).toHaveTextContent(/Ctrl\s*Enter\s*or\s*⌘\s*Enter\s*to confirm/);
    expect(container.querySelectorAll("kbd")).toHaveLength(4);
  });
});
