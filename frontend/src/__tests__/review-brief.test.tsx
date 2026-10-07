// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ReviewPanel } from "@/app/run/[sessionId]/ReviewPanel";
import { SAMPLE_CREATIVE_BRIEF } from "./helpers";

const REPORT = "# Report\n\nJackpot fever.";

function renderCheckpoint1(state: Record<string, unknown> = {}) {
  const onResume = vi.fn();
  render(
    <ReviewPanel
      functionName="review_research"
      sessionState={{
        combined_final_cited_report: REPORT,
        creative_brief: SAMPLE_CREATIVE_BRIEF,
        ...state,
      }}
      onResume={onResume}
    />
  );
  return onResume;
}

const approve = () => fireEvent.click(screen.getByRole("button", { name: /approve/i }));
const lastResponse = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.at(-1)?.[0] as Record<string, unknown>;

describe("checkpoint 1: ReviewBrief", () => {
  it("reviews the editable brief, with the full report collapsed underneath", () => {
    renderCheckpoint1();
    expect(screen.getByRole("heading", { name: "Review creative brief" })).toBeInTheDocument();
    expect(screen.getByLabelText("Single-minded proposition")).toHaveValue(
      SAMPLE_CREATIVE_BRIEF.single_minded_proposition
    );
    expect(screen.getByLabelText("Trend fit (1–5)")).toHaveValue("3");
    expect(screen.getByLabelText("Fit mode")).toHaveValue("cultural");
    const details = screen.getByText("Full research report").closest("details");
    expect(details).not.toHaveAttribute("open");
  });

  it("falls back to the report review for sessions without a brief", () => {
    renderCheckpoint1({ creative_brief: undefined });
    expect(screen.getByRole("heading", { name: "Review research report" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Single-minded proposition")).not.toBeInTheDocument();
  });

  it("approving an untouched brief sends no edits", () => {
    const onResume = renderCheckpoint1();
    approve();
    const response = lastResponse(onResume);
    expect(response.status).toBe("approved");
    expect(response).not.toHaveProperty("edits");
    expect(response).not.toHaveProperty("brief_edited");
    expect(response).not.toHaveProperty("report_edited");
  });

  it("sends the full edited brief as a creative_brief edit with brief_edited", () => {
    const onResume = renderCheckpoint1();
    fireEvent.change(screen.getByLabelText("Single-minded proposition"), {
      target: { value: "Your tone is the jackpot." },
    });
    fireEvent.change(screen.getByLabelText("Trend fit (1–5)"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove mandatory 2" }));
    approve();
    const response = lastResponse(onResume);
    expect(response.brief_edited).toBe(true);
    expect(response).not.toHaveProperty("report_edited");
    expect(response.edits).toEqual([
      {
        field: "creative_brief",
        value: {
          ...SAMPLE_CREATIVE_BRIEF,
          single_minded_proposition: "Your tone is the jackpot.",
          trend_bridge: { ...SAMPLE_CREATIVE_BRIEF.trend_bridge, fit_score: 4 },
          mandatories: ["SE CE24"],
        },
      },
    ]);
  });

  it("blocks submitting a brief without a proposition, with an accessible error", () => {
    const onResume = renderCheckpoint1();
    const smp = screen.getByLabelText("Single-minded proposition");
    fireEvent.change(smp, { target: { value: "   " } });
    expect(smp).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Add the single-minded proposition.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Fix 1 field");
    approve();
    expect(onResume).not.toHaveBeenCalled();
  });

  it("keeps 3-5 angles: remove is disabled at 3, add stops at 5, a new angle needs a name", () => {
    const onResume = renderCheckpoint1();
    expect(screen.getByRole("button", { name: "Remove angle A1" })).toBeDisabled();
    const add = screen.getByRole("button", { name: "Add angle" });
    fireEvent.click(add);
    expect(screen.getByText("Give this angle a name.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Angle A4 name"), { target: { value: "Tone hunt" } });
    fireEvent.click(add);
    fireEvent.change(screen.getByLabelText("Angle A5 name"), { target: { value: "Signal" } });
    expect(add).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove angle A2" }));
    approve();
    const value = (lastResponse(onResume).edits as { value: { angles: { angle_id: string }[] } }[])[0].value;
    expect(value.angles.map((a) => a.angle_id)).toEqual(["A1", "A3", "A4", "A5"]);
  });

  it("edits a reason to believe and its source id", () => {
    const onResume = renderCheckpoint1();
    fireEvent.change(screen.getByLabelText("Reason 2 source id"), { target: { value: "src-3" } });
    fireEvent.click(screen.getByRole("button", { name: "Add reason" }));
    fireEvent.change(screen.getByLabelText("Reason 3 claim"), { target: { value: "Played by pros" } });
    approve();
    const value = (lastResponse(onResume).edits as { value: Record<string, unknown> }[])[0].value;
    expect(value.reasons_to_believe).toEqual([
      SAMPLE_CREATIVE_BRIEF.reasons_to_believe[0],
      { ...SAMPLE_CREATIVE_BRIEF.reasons_to_believe[1], source_id: "src-3" },
      { claim: "Played by pros", source_id: null },
    ]);
  });

  it("also sends a report edit made in the collapsed report", () => {
    const onResume = renderCheckpoint1();
    fireEvent.click(screen.getByRole("button", { name: "Edit report" }));
    fireEvent.change(screen.getByLabelText("Research report"), {
      target: { value: "# Report\n\nJackpot fever, edited." },
    });
    approve();
    const response = lastResponse(onResume);
    expect(response.report_edited).toBe(true);
    expect(response).not.toHaveProperty("brief_edited");
    expect(response.edits).toEqual([
      { field: "combined_final_cited_report", value: "# Report\n\nJackpot fever, edited." },
    ]);
  });
});

describe("checkpoint 2: ad copy review", () => {
  const copies = { ad_copies: [{ headline: "Beep beep, but funnier", body_text: "b" }] };

  it("asks for a one-time revision on the first review", () => {
    const onResume = vi.fn();
    render(
      <ReviewPanel functionName="review_ad_copies" sessionState={{ ad_copy_critique: copies }} onResume={onResume} />
    );
    expect(screen.getByRole("heading", { name: "Review ad copies" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Feedback (optional)"), { target: { value: "funnier" } });
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(lastResponse(onResume)).toMatchObject({ status: "revision_requested", feedback: "funnier" });
  });

  it("shows the revised copies on the second review", () => {
    render(
      <ReviewPanel
        functionName="review_ad_copies"
        sessionState={{ ad_copy_critique: copies, ad_copy_user_revisions_used: 1 }}
        onResume={vi.fn()}
      />
    );
    expect(screen.getByRole("heading", { name: "Review revised ad copies" })).toBeInTheDocument();
    expect(screen.getByText("Beep beep, but funnier")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with feedback" })).toBeDisabled();
  });
});
