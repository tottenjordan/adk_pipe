/**
 * "How to read this" copy for the experiment page's Explain mode: longer than the
 * ⓘ definitions (experiment-help.ts), and about reading the picture rather than
 * defining a term. The data-specific sentence for each panel comes from
 * experiment-insights.ts; this is the part that stays the same for every run.
 */

/** Scoreboard (Overview). */
export const SCOREBOARD_EXPLAIN = {
  board:
    "Each row is one creative, ranked by how much of the endpoint's traffic it gets now. The strip on the right shows that share across the run, from the first round on the left to the last on the right. Every strip uses the same scale, so a taller line means more traffic. The thin grey line marks an even split: above it, the endpoint is favouring that creative.",
  trafficNow: "The creative's share of impressions in the last part of the run.",
  clickRate:
    "Clicks divided by impressions for this creative. The true rate is the simulator's hidden rate, so you can see how close the endpoint's evidence got to the truth.",
  segmentsWon:
    "Reader groups for which this creative is the best choice. A creative can win a group without winning overall traffic.",
} as const;

/** Analysis charts, keyed like CHART_HELP. */
export const CHART_EXPLAIN = {
  avgReward:
    "Read across to the right edge: the higher a line ends, the more reward that strategy earned per round. The gap between a line and the dashed oracle is reward it left on the table. A line that climbs towards the oracle is learning.",
  regret:
    "Every line starts at zero and can only rise. Steep means a strategy is still showing readers the wrong creative; flat means it has stopped losing. Compare where the lines end: lower is better. Where the shaded bands overlap, the strategies are not clearly different yet.",
  optimalShare:
    "This is the same story as regret, in percent: how often each strategy picked the right creative for the reader in front of it. An even split sits at one divided by the number of creatives; the oracle would sit at 100%.",
  trafficShare:
    "These lines are creatives, not strategies. Lines that spread apart mean the endpoint has a preference. Lines that stay bunched mean it can't yet tell the creatives apart, or that different readers prefer different creatives.",
  segments:
    "Each row is a reader group. The best creative column is the simulator's truth; the percentages say how often each strategy showed it. A context-aware strategy should do well in every row, not just on average.",
  totals:
    "Longer bars earned more reward per episode. The whiskers show how much that varied between episodes: when two bars' whiskers overlap a lot, the difference may not hold up.",
  armTable:
    "Compare the two click-rate columns: when the estimate sits close to the true rate, the endpoint has seen enough of that creative to judge it. Creatives with few impressions have looser estimates.",
} as const;
