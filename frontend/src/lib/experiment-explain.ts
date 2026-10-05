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

/** Creative detail drawer (opened from a scoreboard row). */
export const CREATIVE_DETAIL_EXPLAIN = {
  numbers:
    "Lift compares this creative's click rate with the rate of all creatives pooled together. Traffic share against click share shows whether it earns its keep: a click share above its traffic share means it gets more clicks than its slice of impressions would suggest. Missed clicks are the clicks it lost, on average per episode, by being shown to readers another creative would have suited better.",
  overTime:
    "Each point is the click rate in one slice of the run, oldest on the left. The dashed line is the simulator's true rate. Early slices can wander while the creative is shown less; points settling near the dashed line mean the evidence has caught up with the truth.",
  segments:
    "Each row is a reader group, and every bar uses the same scale, so you can compare across groups and across creatives. The dark tick is the true rate for that group. Rows marked best are the groups this creative should be shown to; for the others, the named creative is the better choice.",
  grid:
    "Rows are creatives, columns are reader groups. A darker cell means a higher click rate, on one scale for the whole grid. The outlined cell in each column is the best creative for that group: if the outlines sit in different rows, different readers want different creatives.",
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

/** Runs with scripted shifts (contracts §10). */
export const SHIFT_EXPLAIN = {
  cards:
    "Each card is one shift. The two numbers are how often the endpoint showed readers their best creative in the 2,000 rounds just before the shift and the 2,000 just after it. The round count is how long it took to regain most of its footing: until its best-creative rate over the trailing 1,000 rounds was back to 80% of the pre-shift level. That is most of the way back, not all of it. Read the interval, not just the mean: a wide one means episodes disagreed.",
  markers:
    "The vertical rules mark your shifts, so you can see each line react. A cumulative line bends rather than jumps, because it averages everything since the start. The dashed blue line is the endpoint on the same readers without your shifts: where it pulls away from the solid line, the shift cost reward.",
  periods:
    "With shifts, a creative can be best before a shift and lose that place after it. Switching periods shows each one on its own, so nothing here is a blend of the whole run.",
  forgetting:
    "Forgetting old evidence lets the endpoint recover from a shift faster, because readers from before the shift stop outvoting the new ones. The price is noisier estimates when nothing changes. Compare two runs, one with forgetting on and one off, in the run picker.",
} as const;

/** Background for the "why the endpoint trails" reading (experiment-insights.ts `why`). */
export const TRAILING_EXPLAIN = {
  drift:
    "Linear TS keeps a running summary of every reader it has seen. With full memory, the evidence from before a swap keeps pulling its estimates toward the old winner, and new readers only slowly outweigh it. Forgetting old evidence (a discount) shortens that memory: the endpoint recovers sooner after a change, at the price of noisier estimates while nothing changes. Strategies that track one click rate per creative have less to re-learn, so they can still recover first.",
  context:
    "Linear TS learns how each reader's context (device, time of day, topic and so on) changes each creative's click rate. That pays off when different readers want different creatives. When most readers want the same one, it is extra work: the endpoint keeps exploring to pin down effects that don't matter, while a strategy that only counts clicks per creative settles sooner.",
} as const;
