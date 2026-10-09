// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ShareError, type Share } from "@/lib/shares";

const listShares = vi.fn();
const revokeShare = vi.fn();
vi.mock("@/lib/api", () => ({
  listShares: (...a: unknown[]) => listShares(...a),
  revokeShare: (...a: unknown[]) => revokeShare(...a),
}));

import { SessionShares } from "@/components/shares-list";

const share = (over: Partial<Share> = {}): Share => ({
  token: "AbCdEfGhIjKlMnOp", url: "https://share.example.com/s/AbCdEfGhIjKlMnOp", title: "Acme x Trend",
  scope: "slate", include_eval: false, concept_names: [], app_name: "creative_agent", session_id: "s1",
  created_at: "2026-10-09T10:00:00Z", ...over,
});

const A = share();
const B = share({
  token: "BbbbbbbbbbbbbbbbB", title: "The Reveal", scope: "creative", include_eval: true,
  concept_names: ["The Reveal"], url: "https://share.example.com/s/BbbbbbbbbbbbbbbbB",
});
const OTHER = share({ token: "Cccccccccccccccc", title: "Other run", session_id: "s2" });

const writeText = vi.fn();
beforeEach(() => {
  listShares.mockReset();
  revokeShare.mockReset();
  writeText.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

async function rows() {
  const list = await screen.findByRole("list", { name: "Shared links" });
  return within(list).getAllByRole("listitem");
}

describe("SessionShares", () => {
  it("lists only this session's shares with scope and the checks badge", async () => {
    listShares.mockResolvedValue([B, OTHER, A]);
    render(<SessionShares sessionId="s1" />);
    const items = await rows();
    expect(items).toHaveLength(2);
    expect(screen.queryByText("Other run")).not.toBeInTheDocument();
    expect(within(items[0]).getByText("The Reveal")).toBeInTheDocument();
    expect(within(items[0]).getByText("1 creative")).toBeInTheDocument();
    expect(within(items[0]).getByText("With checks")).toBeInTheDocument();
    expect(within(items[1]).getByText("Slate")).toBeInTheDocument();
    expect(within(items[1]).queryByText("With checks")).not.toBeInTheDocument();
    expect(within(items[1]).getByText(/2026/)).toBeInTheDocument();
  });

  it("renders nothing when this session has no shares", async () => {
    listShares.mockResolvedValue([OTHER]);
    const { container } = render(<SessionShares sessionId="s1" />);
    await waitFor(() => expect(listShares).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a newly created share at the top", async () => {
    listShares.mockResolvedValue([A]);
    const { rerender } = render(<SessionShares sessionId="s1" />);
    await rows();
    rerender(<SessionShares sessionId="s1" created={[B]} />);
    const items = await rows();
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText("The Reveal")).toBeInTheDocument();
  });

  it("copies a row's link and announces it", async () => {
    listShares.mockResolvedValue([A]);
    render(<SessionShares sessionId="s1" />);
    const [row] = await rows();
    await act(async () => {
      fireEvent.click(within(row).getByRole("button", { name: "Copy link to Acme x Trend" }));
    });
    expect(writeText).toHaveBeenCalledWith(A.url);
    expect(screen.getByTestId("shares-copy-status")).toHaveTextContent("Link copied");
  });

  it("revokes after confirmation: calls DELETE and removes the row", async () => {
    listShares.mockResolvedValue([B, A]);
    revokeShare.mockResolvedValue(undefined);
    render(<SessionShares sessionId="s1" />);
    const [first] = await rows();
    fireEvent.click(within(first).getByRole("button", { name: "Revoke The Reveal" }));
    const confirm = await screen.findByRole("dialog");
    expect(within(confirm).getByText(/Revoke this link\?/)).toBeInTheDocument();
    expect(revokeShare).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole("button", { name: "Revoke link" }));
    await waitFor(() => expect(revokeShare).toHaveBeenCalledWith(B.token));
    await waitFor(() => expect(screen.queryByText("The Reveal")).not.toBeInTheDocument());
    expect(await rows()).toHaveLength(1);
  });

  it("cancelling the confirmation keeps the row", async () => {
    listShares.mockResolvedValue([A]);
    render(<SessionShares sessionId="s1" />);
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Revoke Acme x Trend" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(revokeShare).not.toHaveBeenCalled();
    expect(await rows()).toHaveLength(1);
  });

  it("drops a row that was already revoked elsewhere", async () => {
    listShares.mockResolvedValue([A]);
    revokeShare.mockRejectedValue(new ShareError("share_not_found", 404));
    render(<SessionShares sessionId="s1" />);
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Revoke Acme x Trend" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke link" }));
    await waitFor(() => expect(screen.queryByText("Acme x Trend")).not.toBeInTheDocument());
  });

  it("keeps the row with a friendly error when the revoke fails", async () => {
    listShares.mockResolvedValue([A]);
    revokeShare.mockRejectedValue(new ShareError("revoke_incomplete", 502));
    render(<SessionShares sessionId="s1" />);
    const [row] = await rows();
    fireEvent.click(within(row).getByRole("button", { name: "Revoke Acme x Trend" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Revoke link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Revoke again to finish/);
    expect(await rows()).toHaveLength(1);
  });

  it("shows a load error instead of failing silently", async () => {
    listShares.mockRejectedValue(new ShareError("store_failed", 502));
    render(<SessionShares sessionId="s1" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load/i);
  });
});
