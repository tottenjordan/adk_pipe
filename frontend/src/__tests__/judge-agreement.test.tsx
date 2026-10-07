// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { judgeAgreementText, type Calibration, type KappaStats } from "@/lib/ratings";

const calibMock = vi.fn();
vi.mock("@/lib/api", () => ({ getCalibration: () => calibMock() }));

import { JudgeAgreement } from "@/components/judge-agreement";

const kappa = (over: Partial<KappaStats>): KappaStats => ({
  n: 0,
  agreement: null,
  kappa: null,
  reason: "no_pairs",
  ...over,
});

const calib = (judge: Partial<KappaStats>, min = 20): Calibration => {
  const block = {
    n: judge.n ?? 0,
    judge_passed: kappa(judge),
    judge_gates_passed: kappa({}),
    score_spearman: { n: 0, rho: null, reason: "too_few_pairs" },
  };
  return {
    n: block.n,
    sessions: 1,
    ready_min_ratings: min,
    overall: block,
    by_kind: { visual: block, ad_copy: block },
  };
};

describe("judgeAgreementText", () => {
  it("asks for more ratings below the threshold", () => {
    expect(judgeAgreementText(calib({ n: 0 }))).toBe("Rate 20 more creatives to calibrate the judge");
    expect(judgeAgreementText(calib({ n: 19, agreement: 0.9, kappa: 0.8 }))).toBe(
      "Rate 1 more creative to calibrate the judge"
    );
  });
  it("reports agreement and kappa once there are enough pairs", () => {
    expect(judgeAgreementText(calib({ n: 34, agreement: 0.8123, kappa: 0.6234, reason: null }))).toBe(
      "Judge agreement: 81% over 34 ratings, kappa 0.62"
    );
  });
  it("explains an undefined kappa", () => {
    expect(judgeAgreementText(calib({ n: 25, agreement: 0.6, reason: "judge_single_class" }))).toBe(
      "Judge agreement: 60% over 25 ratings, kappa not defined: the judge gave every creative the same verdict"
    );
  });
  it("is null for a malformed report", () => {
    expect(judgeAgreementText(null)).toBeNull();
    expect(judgeAgreementText({} as Calibration)).toBeNull();
  });
});

describe("JudgeAgreement", () => {
  beforeEach(() => {
    calibMock.mockReset();
  });

  it("renders the line from the API", async () => {
    calibMock.mockResolvedValue(calib({ n: 3 }));
    render(<JudgeAgreement />);
    expect(await screen.findByText("Rate 17 more creatives to calibrate the judge")).toHaveClass(
      "text-muted-foreground"
    );
  });

  it("renders nothing when the endpoint fails", async () => {
    calibMock.mockImplementation(() => Promise.reject(new Error("404")));
    const { container } = render(<JudgeAgreement />);
    await waitFor(() => expect(calibMock).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });
});
