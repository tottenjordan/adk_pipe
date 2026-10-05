/**
 * Help copy for the bandit experiment UI ("ⓘ" info popovers), kept in one place so it
 * stays consistent across pages and can be tested. Plain language, sentence case.
 * Accuracy anchors: docs/bandit/contracts.md (batch size 100, CTR modes, reward modes,
 * status enum, TTL reaper).
 */
import type { CtrMode, ExperimentStatus, RewardMode, Scenario } from "@/lib/experiments";

/** Experiment page controls. */
export const CONTROL_HELP = {
  episodes:
    "An episode is one independent run: the endpoint's model is reset and learns from scratch on fresh simulated readers. More episodes give tighter confidence bands.",
  rounds:
    "Each round is one simulated reader seeing one creative. The endpoint updates its model every 100 rounds.",
  startTraffic:
    "Starts a Cloud Run job that sends synthetic readers to the live endpoint and replays baseline strategies on the same readers for comparison.",
  stop: "Stopping deletes the live endpoint and its model so it stops costing money. Results and charts are kept.",
  ttl: "The endpoint is deleted automatically when this timer runs out.",
  status: "What the experiment is doing now.",
} as const;

/** The shift editor and the per-run views (contracts §10). */
export const SHIFT_HELP = {
  section:
    "Script up to four changes in what the simulated readers want, at set points in each episode. Every strategy sees the same changes, so the comparison stays fair, and a dashed line shows the endpoint on the same readers without them.",
  forget:
    "On: for this run the endpoint weighs each batch of readers a little less than the next, remembering roughly the last eighth of an episode, so it can let go of a winner that stopped winning. Off: it keeps every reader at full weight.",
  preview:
    "The click rate each creative should get with each segment in every period between shifts, before random variation. The outlined cell is the segment's best creative in that period.",
  runs: "Each Start traffic is a numbered run with its own shift script. Pick a run to see its results; the newest is shown by default.",
  results:
    "How often the endpoint showed readers their best creative in the 2,000 rounds before and after each shift, and how many rounds it took to regain most of its footing: until its best-creative rate over the trailing 1,000 rounds was back to 80% of the pre-shift level. Numbers are means across episodes with 95% intervals; under five episodes it is too early to call.",
  ghost:
    "Linear TS without your shifts: the same endpoint settings replayed on the same simulated readers with no shifts. The gap to the solid Linear TS line is what the shifts cost.",
  periods:
    "Shifts split the run into periods. Pick one to see who was best for each segment then, instead of a blend of the whole run.",
} as const;

/** Copy for the Stop confirmation step. */
export const STOP_CONFIRM = {
  title: "Stop this experiment?",
  body: "This deletes the live endpoint and its model so it stops costing money. Results and charts are kept.",
  confirm: "Stop and delete endpoint",
  cancel: "Keep it running",
} as const;

/** One line per experiment status (the contract's status enum). */
export const STATUS_HELP: Record<ExperimentStatus, string> = {
  deploying: "Creating the live endpoint and loading its model. This usually takes 10–20 minutes.",
  ready: "The endpoint is live and waiting. Start traffic to simulate readers.",
  running_traffic: "A Cloud Run job is sending simulated readers to the endpoint. Charts update as episodes finish.",
  stopping: "Deleting the endpoint and its model.",
  stopped: "You stopped it. The endpoint was deleted; results are kept.",
  failed: "Something went wrong deploying or running it. Deploy again from the run's results page.",
  expired: "The endpoint reached the end of its lifetime and was deleted automatically. Results are kept.",
};

/** Chart and table panels on the experiment page. */
export const CHART_HELP = {
  avgReward:
    "Each line is a strategy for choosing a creative, not a creative: Linear TS is your live endpoint and the others are baselines replayed on the same readers. The dashed line is the oracle, which always shows each reader their best creative, so it is the ceiling. Rounds use a log scale so early learning and the long run both fit.",
  regret:
    "Regret is the expected clicks (or engaged seconds) lost by not always showing each reader their best creative. Each line is a strategy. Flatter is better: a line that levels off has stopped losing reward. Bands are 95% intervals across episodes.",
  optimalShare:
    "The percentage of rounds where a strategy picked the best creative for that reader. Each line is a strategy; bands are 95% intervals across episodes.",
  trafficShare:
    "Unlike the charts above, each line here is one creative: its share of the live endpoint's impressions over time. Colours match the dots on the creative cards.",
  segments:
    "Segments are synthetic reader groups, each with its own tastes. For each one: its best creative, how often Linear TS chose it, and the best baseline strategy's rate.",
  totals:
    "The expected total reward one episode collects, by strategy. Bars are the mean across episodes; whiskers show one standard deviation either side.",
  armTable:
    "One row per creative. Impressions count how often the live endpoint showed it. Estimated click rate is what the endpoint learned; true click rate is the simulator's hidden rate. Demo mode inflates the rates so learning shows quickly.",
} as const;

/** Creative cards on the experiment page. */
export const CARD_HELP = {
  score: "The LLM judge's overall score for this creative, from the creative evaluation.",
  creativeId: "A stable id for this creative, used in logs and BigQuery tables.",
} as const;

/** Deploy panel on the results page. */
export const SCENARIO_HELP: Record<Scenario, string> = {
  clear_winner: "One creative beats the others for every reader, which shows how quickly the bandit settles on it.",
  segment_winners:
    "Each reader segment has a different best creative, so only a strategy that uses reader context finds them all.",
  drift: "The best creative changes partway through, which shows the bandit noticing and adapting.",
};

export const CTR_MODE_HELP: Record<CtrMode, string> = {
  demo: "Inflated click rates (about 4%) so learning is visible in a short run.",
  realistic: "Real-world click rates (about 0.8%), which need about 10× more traffic to learn from.",
};

export const REWARD_HELP: Record<RewardMode, string> = {
  click: "A click scores 1, anything else 0.",
  engaged: "A click scores the seconds the reader then spends with the brand (click × dwell seconds).",
};

/** Deploy panel "Advanced: tune the simulated readers" (contracts §9). */
export const TUNE_HELP = {
  section:
    "Each scenario fills these in with its own defaults. Change them to make the bandit's job easier or harder; the experiment page marks tuned experiments as custom.",
  mix: "What share of the simulated readers comes from each segment. Moving one slider rebalances the others in proportion, and every segment keeps at least 5%.",
  gap: "How far apart the creatives' click rates are. Subtle gaps take the bandit more traffic to tell apart; obvious gaps are found quickly.",
  judge:
    "How well the eval judge's scores predict real clicks. When the judge is right, the creatives it scored highest also get clicked most. When it is backwards, they get clicked least, and the bandit has to learn that from the readers.",
  noise:
    "Random variation in how each creative lands with each kind of reader, beyond what the judge's scores explain. At zero, the expected rates below are exactly what the simulator uses.",
  drift: "When, as a share of each episode, the best and worst creatives swap places.",
  preview:
    "The click rate each creative should get with each segment under these settings, before random variation. The outlined cell is the segment's best creative.",
  reset: "Put every setting back to the scenario's defaults.",
  custom: "This experiment was deployed with tuned reader settings instead of the scenario's defaults.",
  judgeRight: "Readers agree with the judge: its top-scored creative is the one they click most.",
  judgeNone: "The judge's scores tell the bandit nothing about which creative readers will click.",
  judgeBackwards: "Readers disagree with the judge: its top-scored creative is the one they click least.",
} as const;

export const DEPLOY_HELP = {
  creatives:
    "Pick 2 to 4 creatives. Each becomes one option (arm) the bandit can show; more arms need more traffic to tell apart.",
  scenario: "How the simulated readers respond to the creatives.",
  ctrMode: "How often simulated readers click.",
  reward: "What the bandit is trying to maximise.",
  ttl: "How long the endpoint lives. It is deleted automatically when this runs out, or earlier if you stop it, so it can't keep costing money.",
} as const;
