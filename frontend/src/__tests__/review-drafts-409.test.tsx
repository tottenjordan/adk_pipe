// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReviewPanel } from "@/app/run/[sessionId]/ReviewPanel";
import { clearReviewDrafts } from "@/app/run/[sessionId]/review-drafts";

// A rejected resume (409 not applied / 400) remounts the review panel keyed by
// the paused call's id; every panel's edits must survive that remount.

afterEach(() => clearReviewDrafts());

function renderPanel(
  functionName: string,
  sessionState: Record<string, unknown>,
  functionCallId?: string,
) {
  return render(
    <ReviewPanel
      functionName={functionName}
      functionCallId={functionCallId}
      sessionState={sessionState}
      onResume={vi.fn()}
    />,
  );
}

/** Render with "fc-1", edit, unmount; returns a re-render helper for the same state. */
function editThenRemount(
  functionName: string,
  state: Record<string, unknown>,
  edit: () => void,
) {
  const first = renderPanel(functionName, state, "fc-1");
  edit();
  first.unmount();
  return (id?: string) => renderPanel(functionName, state, id);
}

const type = (el: HTMLElement, value: string) =>
  fireEvent.change(el, { target: { value } });

describe("review drafts survive a rejected-resume remount", () => {
  const RESEARCH = {
    combined_final_cited_report: "# Report\n\nJackpot fever.",
  };

  it("research: feedback and the edited report", () => {
    const rerender = editThenRemount("review_research", RESEARCH, () => {
      type(screen.getByLabelText("Feedback (optional)"), "lean on nostalgia");
      fireEvent.click(screen.getByRole("button", { name: "Edit report" }));
      type(screen.getByLabelText("Research report"), "# Report\n\nEdited.");
    });
    const restored = rerender("fc-1");
    expect(screen.getByLabelText("Feedback (optional)")).toHaveValue(
      "lean on nostalgia",
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit report" }));
    expect(screen.getByLabelText("Research report")).toHaveValue(
      "# Report\n\nEdited.",
    );
    restored.unmount();

    rerender("fc-2");
    expect(screen.getByLabelText("Feedback (optional)")).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "Edit report" }));
    expect(screen.getByLabelText("Research report")).toHaveValue(
      "# Report\n\nJackpot fever.",
    );
  });

  it("ad copies: feedback", () => {
    const state = {
      ad_copy_critique: { ad_copies: [{ headline: "Win big" }] },
    };
    const rerender = editThenRemount("review_ad_copies", state, () =>
      type(screen.getByLabelText("Feedback (optional)"), "less salesy"),
    );
    const restored = rerender("fc-1");
    expect(screen.getByLabelText("Feedback (optional)")).toHaveValue(
      "less salesy",
    );
    restored.unmount();
    rerender("fc-2");
    expect(screen.getByLabelText("Feedback (optional)")).toHaveValue("");
  });

  it("research and ad-copy feedback for the same call id do not collide", () => {
    const first = renderPanel("review_research", RESEARCH, "fc-1");
    type(screen.getByLabelText("Feedback (optional)"), "research note");
    first.unmount();
    renderPanel(
      "review_ad_copies",
      { ad_copy_critique: { ad_copies: [] } },
      "fc-1",
    );
    expect(screen.getByLabelText("Feedback (optional)")).toHaveValue("");
  });

  it("visual concepts: prompt and revision note", () => {
    const state = {
      final_visual_concepts: {
        visual_concepts: [
          {
            concept_name: "Ticket rain",
            image_generation_prompt: "Original prompt",
            aspect_ratio: "9:16",
            visual_style: "Comic panel",
          },
        ],
      },
    };
    const rerender = editThenRemount("review_visual_concepts", state, () => {
      type(screen.getByLabelText("Image prompt"), "Edited prompt");
      type(screen.getByLabelText("Revision note (applied by AI)"), "add a dog");
    });
    const restored = rerender("fc-1");
    expect(screen.getByLabelText("Image prompt")).toHaveValue("Edited prompt");
    expect(screen.getByLabelText("Revision note (applied by AI)")).toHaveValue(
      "add a dog",
    );
    restored.unmount();
    rerender("fc-2");
    expect(screen.getByLabelText("Image prompt")).toHaveValue(
      "Original prompt",
    );
    expect(screen.getByLabelText("Revision note (applied by AI)")).toHaveValue(
      "",
    );
  });

  it("trends: selection and note", () => {
    const state = {
      raw_gtrends: ["Taylor Swift", "World Series", "Powerball"],
    };
    const rerender = editThenRemount("review_trends", state, () => {
      fireEvent.click(screen.getByRole("button", { name: /Taylor Swift/ }));
      fireEvent.click(screen.getByRole("button", { name: /Powerball/ }));
      type(
        screen.getByLabelText("Note for the agent (optional)"),
        "music only",
      );
    });
    const restored = rerender("fc-1");
    expect(
      screen.getByRole("button", { name: /Taylor Swift/ }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Powerball/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByRole("button", { name: /World Series/ }),
    ).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("2 / 3 selected")).toBeInTheDocument();
    expect(screen.getByLabelText("Note for the agent (optional)")).toHaveValue(
      "music only",
    );
    restored.unmount();

    rerender("fc-2");
    expect(screen.getByText("0 / 3 selected")).toBeInTheDocument();
    expect(screen.getByLabelText("Note for the agent (optional)")).toHaveValue(
      "",
    );
  });

  it("without a call id the panels keep plain local state", () => {
    const first = renderPanel("review_ad_copies", {
      ad_copy_critique: { ad_copies: [] },
    });
    type(screen.getByLabelText("Feedback (optional)"), "gone after remount");
    first.unmount();
    renderPanel("review_ad_copies", { ad_copy_critique: { ad_copies: [] } });
    expect(screen.getByLabelText("Feedback (optional)")).toHaveValue("");
  });
});
