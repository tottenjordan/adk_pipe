// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LearnFromRatingsToggle } from "@/components/learn-from-ratings-toggle";

describe("LearnFromRatingsToggle", () => {
  it("renders an unchecked, labelled checkbox by default", () => {
    render(<LearnFromRatingsToggle brand="" checked={false} onChange={() => {}} />);
    const box = screen.getByRole("checkbox", { name: /Learn from past ratings for this brand/ });
    expect(box).not.toBeChecked();
    expect(
      screen.getByText(
        "Uses your team's ratings of earlier creatives for this brand to steer styles, guidance and checks. Off by default.",
      ),
    ).toBeInTheDocument();
  });

  it("names the brand in the help text and reports changes", () => {
    const onChange = vi.fn();
    render(<LearnFromRatingsToggle brand="  PRS " checked={false} onChange={onChange} />);
    expect(
      screen.getByText(
        "Uses your team's ratings of earlier PRS creatives to steer styles, guidance and checks. Off by default.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("reflects the checked state", () => {
    render(<LearnFromRatingsToggle brand="X" checked onChange={() => {}} />);
    expect(screen.getByRole("checkbox")).toBeChecked();
  });
});
