// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ArmImage } from "@/app/experiments/[experimentId]/arm-image";

const arm = { imageUri: "gs://bkt/run/out/The_Jackpot.png" };

function failLoad() {
  fireEvent.error(screen.getByRole("img"));
}

describe("ArmImage", () => {
  it("renders the arm's image through the GCS proxy", () => {
    render(<ArmImage arm={arm} alt="The Jackpot" />);
    expect(screen.getByRole("img", { name: "The Jackpot" }).getAttribute("src")).toContain("The_Jackpot.png");
  });

  it("says the image was removed when a cast arm's image no longer loads", () => {
    render(<ArmImage arm={{ ...arm, consentId: "consentAAAA1" }} alt="The Jackpot" />);
    failLoad();
    expect(screen.getByText("Image removed (consent revoked)")).toBeInTheDocument();
  });

  it("keeps the plain placeholder for an uncast arm or one without an image", () => {
    const { unmount } = render(<ArmImage arm={arm} alt="The Jackpot" />);
    failLoad();
    expect(screen.getByText("Image not rendered")).toBeInTheDocument();
    unmount();
    render(<ArmImage arm={{ imageUri: null, consentId: "consentAAAA1" }} alt="x" />);
    expect(screen.getByText("Image not rendered")).toBeInTheDocument();
    render(<ArmImage arm={null} alt="y" />);
    expect(screen.getAllByText("Image not rendered")).toHaveLength(2);
  });
});
