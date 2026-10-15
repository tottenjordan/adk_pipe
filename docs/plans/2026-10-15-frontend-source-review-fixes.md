# Frontend source-review fixes (R1, R2, C1, C2, C3) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (4 parallel subagents, one per PR, each in its own worktree) to implement this plan.

**Goal:** Fix the two Required accessibility findings and the three correctness findings from `frontend/source_review.md` (2026-10-15).

**Architecture:** Four independent PRs. They own disjoint files, so they run in parallel, each written test-first. The only ordering rule: PR-A (menu) merges before PR-B (outline guard), because PR-B's guard test bans `outline-none` repo-wide and PR-A removes the last two uses.

**Tech Stack:** Next.js 16.4 App Router, React 19.3, @base-ui/react 1.9 (Menu, Tabs), Tailwind 4.3, Vitest 5 + Testing Library (jsdom via per-file `// @vitest-environment jsdom` docblock).

---

## Context

`frontend/code_review.md` (2026-10-12) has **no open Critical or Required items**. All six were fixed in #365–#369 and deployed, and I confirmed each fix in the current code. You chose to plan instead from today's `frontend/source_review.md`:

| ID | Finding | PR |
|---|---|---|
| R1 | "Add shift" menu: `role="menu"` with no arrow keys or focus management, and a ~1.2:1 focus cue (`outline-none` + `bg-muted`) | A |
| R2 | `outline-none` + ring-only focus disappears in forced-colors mode (Tailwind v4 upgrade guide: "replace any usage of `outline-none` with `outline-hidden`") | B |
| C3 | `ui/tabs.tsx` drops `orientation` before Base UI's Root | B |
| C1 | `pollRun` returns normally when aborted, so callers run `setStatus("completed")`; the 1.5 s between-poll wait isn't abortable | C1 |
| C2 | No `error.tsx` anywhere: render crashes (including on the public share viewer) show Next's built-in error screen | C2 |

**Out of scope:** the 12 other Consider items in `source_review.md`.

## Code-quality assessment (`code-review-and-quality`, five axes) of the code being changed

| Axis | Finding in current code | Remedy in this plan |
|---|---|---|
| Correctness | `pollRun`'s `return` means two things, "run finished" and "aborted", so both callers mis-set `completed` (C1). `Tabs` silently drops a prop (C3). | Give `return` one meaning: an abort throws (`pollWait`). Pass the prop through. |
| Readability / architecture | `shift-timeline.tsx` is 834 lines. The hand-rolled menu re-implements outside-click, Escape and ids that Base UI already provides. | Extract to `add-shift-menu.tsx` on Base UI Menu, which **deletes** the bespoke effect and listeners (the file shrinks, nothing just moves). |
| Accessibility (correctness for keyboard and AT users) | 21 `outline-none` sites, one menu without keyboard support (R1/R2) | Swap to `outline-hidden` in one sed pass, plus a guard test so it can't regress |
| Security | The public share viewer falls back to Next's default error screen. No leak today, but uncontrolled | The share `error.tsx` never renders `error.message` or the digest, and a test pins that |
| Performance | Abort during the 1.5 s poll wait still fires one more fetch | The wait is abortable |

No new dependencies. Each PR is about 30–150 changed lines.

## Orchestration (minimal wall-clock)

Facts that set the schedule:
- `main` has **no branch protection**, so merging one PR never forces the others to rebase or re-run CI.
- `frontend-tests.yml` takes **about 2 min** (last 6 runs: 1:18–2:02).
- The four PRs touch **disjoint files**.

So the critical path is just the slowest PR.

```
T+0   dispatch 4 implementer subagents at once (Agent tool, isolation: "worktree"), one per PR
T+12  C1, C2 done → each opens its PR; reviewer subagent + CI start immediately, in parallel
T+15  B done → PR opened (marked "merge after A"); reviewer + CI in parallel
T+16  C1, C2 green + reviewed → merge (no rebase needed)
T+30  A done → PR, reviewer + CI in parallel
T+35  A merged → B: `git rebase origin/main` (no conflicts, disjoint files) → CI 2 min
T+38  B merged → start the web+share deploy immediately
T+50  deployed, traffic moved, browser check done
```

- **Why B waits for A (a ~3 min cost, accepted on purpose):** B's guard bans `outline-none` everywhere, and the last two uses sit inside the menu that A deletes. Each alternative costs more:
  - a temporary exemption list makes A edit B's test;
  - a separate guard PR adds another review and CI cycle.
- **Local gate per PR, fast:** from `frontend/`, run `npx vitest run <changed tests> && npx eslint <changed files> && npx tsc --noEmit`, about 30–60 s. CI runs the full suite and `next build`, so neither is repeated locally.
- **Reviews never block an implementer:** a fresh reviewer subagent (five-axis review plus the PR's mutation check) runs while CI runs. Findings are fixed on the same branch.
- **Common to every PR:**
  - Branch names are given below.
  - Commit messages are Conventional Commits with **no `Co-Authored-By` trailer** (CODE_STANDARDS.md and the user's rule).
  - PR body ends with no attribution line.
  - Merge only on green `frontend-tests.yml`. Delete a branch only after `gh pr view N --json state` prints `MERGED`, as a separate step.

---

### PR-A: Rebuild "Add shift" on Base UI Menu (R1)

**Branch:** `fix/a11y-add-shift-menu`
**Files:**
- Create: `frontend/src/app/experiments/[experimentId]/add-shift-menu.tsx`
- Create: `frontend/src/__tests__/add-shift-menu.test.tsx`
- Modify: `frontend/src/app/experiments/[experimentId]/shift-timeline.tsx`. Delete the `// ── Add menu` section (`AddShiftMenu`, about lines 172–258) and import the new component. Also swap the `CreativeChip` button's `outline-none` to `outline-hidden` (about line 649): **this PR owns the whole file**.

`shift-timeline.tsx` is 834 lines, so the menu moves to its own file first and then gets rewritten. That way the change shrinks the file instead of growing it.

**Step 1: Write the failing tests** (`add-shift-menu.test.tsx`, jsdom). Interaction patterns come from `src/__tests__/info-tip.test.tsx` (pointerDown + focus + click) and `share-view.test.tsx` (Base UI moves focus in a microtask, so wrap `keyDown` in `await act(async () => …)`).

```tsx
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

  it("lists every kind and preset as menu items", async () => {
    const { trigger } = setup();
    await openWithKeyboard(trigger);
    expect(screen.getAllByRole("menuitem")).toHaveLength(SHIFT_KINDS.length + SHIFT_PRESETS.length);
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
```
Also assert the kind label for item 0 (`KIND_LABELS[SHIFT_KINDS[0]]`) so the order is pinned. If jsdom doesn't open on `ArrowDown`, use the info-tip `pointerDown` → `focus` → `click` sequence to open, then test the arrow keys.

**Step 2:** `npx vitest run src/__tests__/add-shift-menu.test.tsx` should FAIL (the module doesn't exist).

**Step 3: Implement `add-shift-menu.tsx`** using Base UI primitives directly. It's the only menu in the app, so there's no new `ui/` wrapper (YAGNI). The API was checked against `node_modules/@base-ui/react/menu`: `Menu.Root/Trigger/Portal/Positioner/Popup/Group/GroupLabel/Item/Separator`, items expose `data-highlighted`, and the trigger sets `aria-haspopup="menu"`.

```tsx
"use client";

import { Menu } from "@base-ui/react/menu";
import { PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ShiftKind } from "@/lib/experiments";
import { KIND_LABELS, SHIFT_KINDS, SHIFT_PRESETS, type ShiftPresetId } from "@/lib/shifts";

// Items keep the global :focus-visible outline (globals.css), pulled inside the
// item, so keyboard focus stays visible in forced-colors mode too.
const ITEM =
  "block w-full cursor-default rounded-sm px-2.5 py-1.5 text-left text-foreground select-none focus-visible:outline-offset-[-2px] data-highlighted:bg-muted";
const GROUP_LABEL = "px-2.5 pt-1.5 pb-1 text-xs text-muted-foreground";

/**
 * The "Add shift" menu: four kinds and three ready-made presets. Base UI Menu
 * supplies the menu semantics: arrow keys, typeahead, focus moved into the menu
 * and back to the trigger on Escape.
 */
export function AddShiftMenu({
  onAdd,
  disabled,
}: {
  onAdd: (kind: ShiftKind | null, preset: ShiftPresetId | null) => void;
  disabled: boolean;
}) {
  return (
    <Menu.Root modal={false}>
      <Menu.Trigger disabled={disabled} render={<Button variant="outline" size="sm" />}>
        <PlusIcon /> Add shift
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner side="bottom" align="end" sideOffset={4} className="z-50">
          <Menu.Popup className="w-72 rounded-md border border-border bg-popover p-1 text-sm text-popover-foreground shadow-md outline-hidden">
            <Menu.Group>
              <Menu.GroupLabel className={GROUP_LABEL}>What changes</Menu.GroupLabel>
              {SHIFT_KINDS.map((k) => (
                <Menu.Item key={k} className={ITEM} onClick={() => onAdd(k, null)}>
                  {KIND_LABELS[k]}
                </Menu.Item>
              ))}
            </Menu.Group>
            <Menu.Separator className="my-1 border-t border-border" />
            <Menu.Group>
              <Menu.GroupLabel className={GROUP_LABEL}>Ready-made</Menu.GroupLabel>
              {SHIFT_PRESETS.map((p) => (
                <Menu.Item key={p.id} label={p.label} className={ITEM} onClick={() => onAdd(null, p.id)}>
                  <span className="block">{p.label}</span>
                  <span className="block text-xs text-muted-foreground">{p.description}</span>
                </Menu.Item>
              ))}
            </Menu.Group>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
```
In `shift-timeline.tsx`:
- add `import { AddShiftMenu } from "./add-shift-menu";`;
- delete the old component;
- drop the imports that are now unused (lint will name them: likely `PlusIcon`, `useId`/`useRef` if nothing else uses them, `SHIFT_KINDS`, `SHIFT_PRESETS`, `KIND_LABELS`);
- leave the call site at line 104 unchanged.

**Step 4:** Run the new test file to PASS, then the fast local gate.
**Step 5: Mutation check.** Swap `onAdd(k, null)` to `onAdd(null, null)`: the test must go red. Restore it.
**Step 6:** Commit `fix(experiments): rebuild the Add shift menu on Base UI Menu`. Open the PR and quote R1 in the body.

---

### PR-B: `outline-hidden` everywhere + guard; Tabs orientation (R2, C3)

**Branch:** `fix/a11y-forced-colors-focus`. **Must merge after PR-A** (rebase on main first).
**Files:**
- Create: `frontend/src/__tests__/no-outline-none.test.ts`, `frontend/src/__tests__/tabs-orientation.test.tsx`
- Modify: every `src/**/*.tsx` that uses `outline-none` **except** `shift-timeline.tsx`. That's 18 sites, for example `components/run-list.tsx:125`, `components/ui/slider.tsx:76`, `components/ui/info-tip.tsx:55,69`, `app/experiments/[experimentId]/segment-grid.tsx:117`, `app/layout.tsx:39`, `components/ui/dialog.tsx:56`.
- Modify: `frontend/src/components/ui/tabs.tsx:8-22`
- Add: `docs/plans/2026-10-15-frontend-source-review-fixes.md`, a copy of this plan, kept per the writing-plans convention.

**Step 1: Write the failing guard test**, modelled on `src/__tests__/no-entrance-animations.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";

// Tailwind v4: `outline-none` really removes the outline, so ring-only focus
// styles vanish in forced-colors (Windows high-contrast) mode. Use
// `outline-hidden` (transparent outline, shown in forced colors).
// https://tailwindcss.com/docs/upgrade-guide#renamed-outline-utility
const SRC = path.resolve(__dirname, "..");

describe("focus styles", () => {
  it("never use outline-none", () => {
    const offenders = (readdirSync(SRC, { recursive: true }) as string[])
      .filter((f) => /\.tsx?$/.test(f) && !f.includes("__tests__"))
      .filter((f) => /\boutline-none\b/.test(readFileSync(path.join(SRC, f), "utf8")));
    expect(offenders).toEqual([]);
  });
});
```
And the Tabs test (jsdom), checking the exported names in `ui/tabs.tsx` first:
```tsx
it("passes orientation to Base UI (aria-orientation + arrow keys)", () => {
  render(
    <Tabs orientation="vertical" defaultValue="a">
      <TabsList><TabsTrigger value="a">A</TabsTrigger><TabsTrigger value="b">B</TabsTrigger></TabsList>
    </Tabs>
  );
  expect(screen.getByRole("tablist")).toHaveAttribute("aria-orientation", "vertical");
});
```
**Step 2:** Both should FAIL: the guard lists 19 files, and `aria-orientation` is `horizontal`.

**Step 3: Implement.**
- From `frontend/`: `grep -rl "outline-none" src --include=*.tsx | grep -v shift-timeline | xargs sed -i 's/outline-none/outline-hidden/g'`. This also covers the `focus:` and `focus-visible:` forms. Containers that only take programmatic focus (`layout.tsx` main, `ReviewPanel.tsx:596`, the dialog popup) switch too. That's correct in forced colors: the user sees where focus landed. There's no visual change in normal mode.
- In `tabs.tsx`, add `orientation={orientation}` to `<TabsPrimitive.Root …>`. Same pattern as `ui/scroll-area.tsx:39-40`.

**Step 4:** The guard passes except for `shift-timeline.tsx` until PR-A lands. Run the gate on a branch rebased onto main *after* PR-A merges. Before that, check locally with `git merge --no-commit origin/fix/a11y-add-shift-menu`.
**Step 5: Mutation check.** Revert the tabs line and the test goes red; reintroduce one `outline-none` and the guard goes red.
**Step 6:** Two commits: `fix(a11y): use outline-hidden so focus survives forced-colors mode` and `fix(ui): pass Tabs orientation through to Base UI`.

---

### PR-C1: An aborted poll never reads as "completed" (C1)

**Branch:** `fix/run-poll-abort`
**Files:** modify `frontend/src/lib/api.ts` (`pollRun`, about lines 236–276) and `frontend/src/__tests__/poll-run.test.ts` (around line 313).

**Design:** `pollRun` returning means the run finished, and nothing else. An abort rejects with the signal's `AbortError`. Both callers in `use-run-follower.ts:216-241,247-290` already return quietly on `signal.aborted` or `AbortError`, so neither needs a change.

**Step 1: Tests.** Rewrite the existing "stops quietly when aborted during a backoff wait" and add a between-polls case:
```ts
it("rejects with an AbortError when aborted during a backoff wait", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn(async () => unavailable());
  vi.stubGlobal("fetch", fetchMock);
  const controller = new AbortController();
  const next = pollRun("creative_agent", "u1", "s1", { intervalMs: 0, signal: controller.signal }).next();
  const settled = next.then(() => null, (e: unknown) => e);
  await vi.advanceTimersByTimeAsync(500);
  controller.abort();
  expect(await settled).toMatchObject({ name: "AbortError" });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("stops between polls on abort, without another fetch", async () => {
  vi.useFakeTimers();
  const fetchMock = vi.fn(async () => jsonResponse({ status: "running", events: [], nextCursor: 0 }));
  vi.stubGlobal("fetch", fetchMock);
  const controller = new AbortController();
  const next = pollRun("creative_agent", "u1", "s1", { intervalMs: 1500, signal: controller.signal }).next();
  const settled = next.then(() => null, (e: unknown) => e);
  await vi.advanceTimersByTimeAsync(100); // first poll done, inside the 1.5 s wait
  controller.abort();
  await vi.advanceTimersByTimeAsync(5000);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(await settled).toMatchObject({ name: "AbortError" });
});
```
**Step 2:** Run `npx vitest run src/__tests__/poll-run.test.ts`. Both should FAIL: the first resolves `{done:true}`, the second keeps fetching.
**Step 3: Implement** next to the existing `sleep()` (`api.ts:203`):
```ts
/** Wait between polls; an abort rejects with the signal's AbortError, so a
 * caller never reads an aborted poll as a finished run. */
async function pollWait(ms: number, signal?: AbortSignal): Promise<void> {
  if (!(await sleep(ms, signal))) signal?.throwIfAborted();
}
```
Then make two replacements in `pollRun`:
- The backoff branch: `const completed = await sleep(…); if (!completed) return; continue;` becomes `await pollWait(pollBackoffMs(failures), opts.signal); continue;`.
- The tail: `await new Promise((r) => setTimeout(r, intervalMs));` becomes `await pollWait(intervalMs, opts.signal);`.

Update `pollRun`'s doc comment if it says an abort stops quietly.
**Step 4:** The file passes, then the fast local gate. `use-run-follower.test.tsx` and `run-page-connection.test.tsx` must stay green.
**Step 5: Mutation check.** Make `pollWait` `return` instead of throwing, and both new tests must go red.
**Step 6:** Commit `fix(run): reject an aborted poll instead of ending it as completed`.

---

### PR-C2: Route error boundaries (C2)

**Branch:** `fix/route-error-boundaries`
**Files:** create `frontend/src/app/error.tsx`, `frontend/src/app/s/[token]/error.tsx` and `frontend/src/__tests__/error-boundaries.test.tsx`.

**Docs:** `node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/error.md`:
- an error boundary must be a Client Component;
- its props are `error: Error & { digest?: string }` and **`retry`**, which has been stable since 16.3 and is preferred over `reset`.

`global-error.tsx` is skipped (YAGNI): `layout.tsx` does no data work.

**Step 1: Test** (jsdom), for both components:
```tsx
it.each([["app", AppError], ["share", ShareError]])("%s error boundary offers a retry and hides internals", (_n, Boundary) => {
  const retry = vi.fn();
  render(<Boundary error={Object.assign(new Error("gs://secret/path failed"), { digest: "abc123" })} retry={retry} />);
  expect(screen.getByRole("heading")).toBeInTheDocument();
  expect(screen.queryByText(/gs:\/\/secret/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /try again/i }));
  expect(retry).toHaveBeenCalledTimes(1);
});
```
Stub `console.error` with `vi.spyOn(console, "error").mockImplementation(() => {})`.
**Step 2:** FAIL (no modules).
**Step 3: Implement.**
- **`app/error.tsx`:** `"use client"`.
  - `useEffect(() => console.error(error), [error])`.
  - An `<h1>` "Something went wrong showing this page".
  - The text: "Your run carries on on the server. Try again, or reload." plus an optional `Reference: {error.digest}` line.
  - A `<Button onClick={() => retry()}>Try again</Button>`.
  - Lay it out like `s/[token]/not-found.tsx` (`mx-auto flex max-w-xl flex-col gap-3 px-6 py-24`).
- **`app/s/[token]/error.tsx`:** the same structure with share wording: "This page couldn't load right now" plus a Try again button. No digest, since this is the public surface.
- Neither component ever renders `error.message`.

**Step 4:** PASS, then the fast local gate (`tsc --noEmit` type-checks the special file's props; CI's `next build` checks them against the generated route types).
**Step 5: Mutation check.** Drop the `onClick` and the test goes red.
**Step 6:** Commit `feat(frontend): add route error boundaries for the app and the share viewer`.

---

## Review, merge, deploy

1. Reviews run as described in Orchestration: one reviewer subagent per PR, alongside CI, using the five-axis checklist and the PR's mutation check.
2. Merge each PR the moment it is green and reviewed. The only constraint: B rebases after A merges.
3. **One deploy for all four.** Follow `deployment/README.md`:
   - build the image from main via root `cloudbuild.yaml` (`SHORT_SHA=$(git rev-parse --short HEAD)`);
   - `gcloud run deploy trend-trawler-web --image <digest> --no-traffic`, then `update-traffic --to-revisions <new>=100` (web is traffic-pinned);
   - deploy `trend-trawler-share` with the same digest.
   - A web revision change doesn't affect api runs, so no idle check is needed.
4. Update the local `frontend/source_review.md` header (untracked) with the PR numbers and revisions, and the memory runbook with the new web and share revisions.

## Verification

- **Per PR:** the fast local gate, then CI `frontend-tests.yml` green (full lint, test and `next build`), and every mutation check goes red.
- **After the last merge:** one `cd frontend && npm test && npm run build` on main before deploying. It runs while Cloud Build builds the image, so it costs no wall-clock time.
- **Browser, after the deploy** (`playwright-skill`, or manually):
  - On `/experiments/<id>`:
    - Tab to "Add shift", press ↓, and focus lands on the first item;
    - ↑/↓ move focus with a visible outline;
    - Enter adds the shift;
    - Escape returns focus to the trigger.
  - Emulate `forced-colors: active` (Playwright `page.emulateMedia({ forcedColors: "active" })`) and Tab through the run list and a slider: focus outlines are visible.
  - Open `/s/<bad-token>`: the not-found page still renders, so the error boundary doesn't swallow `notFound()`.
