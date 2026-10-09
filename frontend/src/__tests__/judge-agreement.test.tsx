// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  judgeAgreementText,
  learningSplitText,
  type Calibration,
  type CalibrationBlock,
  type KappaStats,
} from "@/lib/ratings";

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

const block = (judge: Partial<KappaStats>): CalibrationBlock => ({
  n: judge.n ?? 0,
  judge_passed: kappa(judge),
  judge_gates_passed: kappa({}),
  score_spearman: { n: 0, rho: null, reason: "too_few_pairs" },
});

const calib = (judge: Partial<KappaStats>, min = 20, over: Partial<Calibration> = {}): Calibration => {
  const b = block(judge);
  return {
    n: b.n,
    sessions: 1,
    ready_min_ratings: min,
    overall: b,
    by_kind: { visual: b, ad_copy: b },
    ...over,
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
  it("notes ratings from earlier judge versions that were not counted", () => {
    const current = { judge_version: "2026-10-08", excluded_other_versions: 7 };
    expect(judgeAgreementText(calib({ n: 5 }, 20, current))).toBe(
      "Rate 15 more creatives to calibrate the judge (current judge only; 7 earlier ratings not counted)"
    );
    expect(
      judgeAgreementText(calib({ n: 30, agreement: 0.9, kappa: 0.7, reason: null }, 20, {
        excluded_other_versions: 1,
      }))
    ).toBe("Judge agreement: 90% over 30 ratings, kappa 0.70 (current judge only; 1 earlier rating not counted)");
    expect(judgeAgreementText(calib({ n: 5 }, 20, { excluded_other_versions: 0 }))).toBe(
      "Rate 15 more creatives to calibrate the judge"
    );
  });
});

describe("learningSplitText", () => {
  const split = (learned: Partial<KappaStats>, notLearned: Partial<KappaStats>, min = 20) =>
    calib({ n: 50 }, min, { by_learning: { learned: block(learned), not_learned: block(notLearned) } });

  it("compares kappa once both learned and not-learned runs have enough pairs", () => {
    expect(learningSplitText(split({ n: 20, kappa: 0.712, reason: null }, { n: 31, kappa: 0.5, reason: null }))).toBe(
      "Learned runs: kappa 0.71 · not learned: kappa 0.50"
    );
    expect(learningSplitText(split({ n: 20, reason: "judge_single_class" }, { n: 20, kappa: 0.4, reason: null }))).toBe(
      "Learned runs: kappa n/a · not learned: kappa 0.40"
    );
  });
  it("is null until both sides have enough pairs, or for an older API", () => {
    expect(learningSplitText(split({ n: 19, kappa: 0.7, reason: null }, { n: 40, kappa: 0.5, reason: null }))).toBeNull();
    expect(learningSplitText(split({ n: 40, kappa: 0.7, reason: null }, { n: 3, kappa: 0.5, reason: null }))).toBeNull();
    expect(learningSplitText(calib({ n: 50 }))).toBeNull();
    expect(learningSplitText(null)).toBeNull();
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

  it("adds a muted learning-split line when both sides have enough pairs", async () => {
    calibMock.mockResolvedValue(
      calib({ n: 45, agreement: 0.8, kappa: 0.6, reason: null }, 20, {
        excluded_other_versions: 2,
        by_learning: {
          learned: block({ n: 20, kappa: 0.65, reason: null }),
          not_learned: block({ n: 25, kappa: 0.55, reason: null }),
        },
      })
    );
    render(<JudgeAgreement />);
    expect(
      await screen.findByText(
        "Judge agreement: 80% over 45 ratings, kappa 0.60 (current judge only; 2 earlier ratings not counted)"
      )
    ).toHaveClass("text-muted-foreground");
    expect(screen.getByText("Learned runs: kappa 0.65 · not learned: kappa 0.55")).toHaveClass(
      "text-muted-foreground"
    );
  });

  it("shows no learning-split line without enough pairs on both sides", async () => {
    calibMock.mockResolvedValue(calib({ n: 3 }));
    render(<JudgeAgreement />);
    await screen.findByText("Rate 17 more creatives to calibrate the judge");
    expect(screen.queryByText(/Learned runs/)).toBeNull();
  });

  it("renders nothing when the endpoint fails", async () => {
    calibMock.mockImplementation(() => Promise.reject(new Error("404")));
    const { container } = render(<JudgeAgreement />);
    await waitFor(() => expect(calibMock).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });
});
