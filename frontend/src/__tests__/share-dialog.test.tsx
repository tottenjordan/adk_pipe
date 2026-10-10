// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ShareError, type Share } from "@/lib/shares";

const createShare = vi.fn();
vi.mock("@/lib/api", () => ({
  createShare: (...a: unknown[]) => createShare(...a),
}));

import type { ReactNode } from "react";
import { ShareDialog } from "@/components/share-dialog";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";
import { buildProofs, type Proof, type VisualConcept } from "@/lib/eval-matching";

const share = (over: Partial<Share> = {}): Share => ({
  token: "AbCdEfGhIjKlMnOp", url: "https://share.example.com/s/AbCdEfGhIjKlMnOp", title: "Acme x Trend",
  scope: "slate", include_eval: false, concept_names: [], app_name: "creative_agent", session_id: "s1",
  created_at: "2026-10-09T10:00:00Z", ...over,
});

const writeText = vi.fn();
beforeEach(() => {
  createShare.mockReset();
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});
afterEach(() => vi.restoreAllMocks());

function open(label = "Share slate") {
  fireEvent.click(screen.getByRole("button", { name: label }));
}

describe("ShareDialog", () => {
  it("creates a slate link without concept_names, eval off by default", async () => {
    createShare.mockResolvedValue(share());
    const onCreated = vi.fn();
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" onCreated={onCreated} />);
    open();
    expect(await screen.findByText(/Anyone with the link can view these creatives and copy\./)).toBeInTheDocument();
    expect(screen.getByText(/Prompts, notes and ratings are never shared\./)).toBeInTheDocument();
    const box = screen.getByRole("checkbox", { name: "Include judge checks and scores" });
    expect(box).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Create link" }));
    await waitFor(() => expect(createShare).toHaveBeenCalledTimes(1));
    expect(createShare).toHaveBeenCalledWith("creative_agent", "s1", { include_eval: false });
    expect(await screen.findByDisplayValue("https://share.example.com/s/AbCdEfGhIjKlMnOp")).toHaveAttribute("readonly");
    expect(onCreated).toHaveBeenCalledWith(share());
    const openLink = screen.getByRole("link", { name: /Open/ });
    expect(openLink).toHaveAttribute("href", "https://share.example.com/s/AbCdEfGhIjKlMnOp");
    expect(openLink).toHaveAttribute("target", "_blank");
    expect(openLink).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("creates a single-creative link with concept_names and include_eval toggled on", async () => {
    createShare.mockResolvedValue(share({ scope: "creative", include_eval: true, concept_names: ["The Reveal"] }));
    render(
      <ShareDialog appName="creative_agent" sessionId="s1" conceptNames={["The Reveal"]} triggerLabel="Share this creative" />
    );
    open("Share this creative");
    fireEvent.click(await screen.findByRole("checkbox", { name: "Include judge checks and scores" }));
    fireEvent.click(screen.getByRole("button", { name: "Create link" }));
    await waitFor(() =>
      expect(createShare).toHaveBeenCalledWith("creative_agent", "s1", { concept_names: ["The Reveal"], include_eval: true })
    );
  });

  it("says how many creatives showing a person were left out of a slate", async () => {
    createShare.mockResolvedValue(
      share({ skipped: [{ concept_name: "The Reveal", reason: "person_not_shareable" }] })
    );
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    expect(
      await screen.findByText(
        "1 creative showing a person was left out (their consent doesn't cover public links)."
      )
    ).toBeInTheDocument();
  });

  it("shows no left-out notice when nothing was skipped", async () => {
    createShare.mockResolvedValue(share({ skipped: [] }));
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    await screen.findByDisplayValue("https://share.example.com/s/AbCdEfGhIjKlMnOp");
    expect(screen.queryByText(/left out/)).not.toBeInTheDocument();
  });

  it("copies the link and announces it in a polite live region", async () => {
    createShare.mockResolvedValue(share());
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    const copy = await screen.findByRole("button", { name: "Copy link" });
    const live = screen.getByTestId("share-copy-status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("");
    await act(async () => {
      fireEvent.click(copy);
    });
    expect(writeText).toHaveBeenCalledWith("https://share.example.com/s/AbCdEfGhIjKlMnOp");
    expect(live).toHaveTextContent("Link copied");
  });

  it("copies a relative url as given and hints that the share service url is unset", async () => {
    createShare.mockResolvedValue(share({ url: "/s/AbCdEfGhIjKlMnOp" }));
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    const copy = await screen.findByRole("button", { name: "Copy link" });
    await act(async () => {
      fireEvent.click(copy);
    });
    expect(writeText).toHaveBeenCalledWith("/s/AbCdEfGhIjKlMnOp");
    expect(screen.getByText(/Share service URL not configured/)).toBeInTheDocument();
  });

  it.each([
    ["no_images", 400, /no rendered images/i],
    ["too_many_shares", 429, /too many active links/i],
    ["shares_unconfigured", 503, /isn't set up/i],
    ["session_not_found", 404, /could not be found/i],
    ["image_outside_bucket", 400, /project bucket/i],
  ])("shows a friendly inline error for %s", async (reason, status, message) => {
    createShare.mockRejectedValue(new ShareError(reason, status));
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    // the user can retry
    expect(screen.getByRole("button", { name: "Create link" })).toBeEnabled();
  });

  it("returns focus to the trigger on close", async () => {
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    const trigger = screen.getByRole("button", { name: "Share slate" });
    trigger.focus();
    fireEvent.click(trigger);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("starts fresh when reopened", async () => {
    createShare.mockResolvedValue(share());
    render(<ShareDialog appName="creative_agent" sessionId="s1" triggerLabel="Share slate" />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    await screen.findByRole("button", { name: "Copy link" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    open();
    expect(await screen.findByRole("button", { name: "Create link" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy link" })).not.toBeInTheDocument();
  });
});

describe("ProofDetail share slot", () => {
  const concept = {
    concept_name: "Dust", headline: "Beep beep", ad_copy_id: 1, concept_summary: "Skates",
  } as VisualConcept;
  function renderDetail(shareSlot?: (p: Proof) => ReactNode) {
    render(
      <ProofDetail
        proofs={buildProofs([concept], [], null)}
        open
        index={0}
        onIndexChange={() => {}}
        onClose={() => {}}
        imageUrlFor={() => null}
        returnFocusTo={() => null}
        shareSlot={shareSlot}
      />
    );
  }
  it("renders the creative's share action and sends its concept name", async () => {
    createShare.mockResolvedValue(share({ scope: "creative", concept_names: ["Dust"] }));
    renderDetail((p) => (
      <ShareDialog
        appName="creative_agent"
        sessionId="s1"
        conceptNames={[p.concept.concept_name]}
        triggerLabel="Share this creative"
      />
    ));
    fireEvent.click(await screen.findByRole("button", { name: "Share this creative" }));
    fireEvent.click(await screen.findByRole("button", { name: "Create link" }));
    await waitFor(() =>
      expect(createShare).toHaveBeenCalledWith("creative_agent", "s1", { concept_names: ["Dust"], include_eval: false })
    );
  });
  it("renders nothing extra when the slot returns null", async () => {
    renderDetail(() => null);
    await screen.findByRole("dialog");
    expect(screen.queryByRole("button", { name: "Share this creative" })).not.toBeInTheDocument();
  });
});
