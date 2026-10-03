// Regenerate the docs/screenshots/*.png from committed fixtures.
//
// These captures are DETERMINISTIC and need no GCP credentials or quota: every
// backend call (`/api/adk/**` and `/api/gcs?**`) is route-mocked from the
// fixtures in ./screenshot-fixtures/, which were harvested from ONE real
// `creative_agent` run of the Paul Reed Smith / SE CE24 / Powerball campaign.
// So 03-results-creative.png shows the ACTUAL generated concept images (the
// downscaled real renders in ./screenshot-fixtures/images/), and all four
// screens reflect the same campaign.
//
// Usage (dev or production build both work — the run page is StrictMode-safe):
//   cd frontend
//   npm run dev                                  # in one terminal
//   npm run screenshots                          # in another
//
// The run screens cover both start paths: 02 has the stored kick-off message
// (kick-off + poll), 04 and 06 have none (view mode: the page asks the server
// for the run and replays it).
//
// Env overrides: SCREENSHOT_BASE_URL (default http://localhost:3000).
//
// Production build (no Next dev badge) — what the committed images use:
//   npm run build
//   cp -r .next/static .next/standalone/.next/static && cp -r public .next/standalone/public
//   PORT=3600 node .next/standalone/server.js     # in one terminal
//   SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots
//
// JOURNEY=1 switches to user-journey mode: instead of the full-page
// docs/screenshots/*.png, it captures a sequence of 1440x900 VIEWPORT frames
// walking the interactive flow (brief → research → three reviews → complete →
// results → proof detail → research report → history) into JOURNEY_OUT
// (default /tmp/tt-journey-frames) plus a journey-frames.json manifest:
//   [{file, step, phase, title, caption, hold_ms, highlights: [{x, y, w, h, label}]}]
// Each highlight is a key UI element's box in 1440x900 viewport CSS px (Playwright
// boundingBox(), padded and clipped to the viewport; max 2 per frame; a locator
// that isn't found is logged and skipped). scripts/build-journey-gif.py turns them
// into docs/screenshots/user-journey.gif with spotlights, callouts and a progress strip:
//   JOURNEY=1 SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots
//   uv run --no-project --with pillow python scripts/build-journey-gif.py
//
// JOURNEY=experiments captures the second journey instead: the first LIVE bandit
// experiment from deploy to stop (see journeyExperiments below) →
// docs/screenshots/experiments-journey.gif.

import { chromium } from "playwright";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIX = join(__dirname, "screenshot-fixtures");
const OUT = join(__dirname, "..", "..", "docs", "screenshots");
const BASE = process.env.SCREENSHOT_BASE_URL ?? "http://localhost:3000";

// ── Load fixtures (harvested from the real run) ───────────────────────────────
const state = JSON.parse(readFileSync(join(FIX, "creative-state.json"), "utf8"));
const events = JSON.parse(readFileSync(join(FIX, "creative-events.json"), "utf8"));
const evalReport = JSON.parse(
  readFileSync(join(FIX, "creative-eval-report.json"), "utf8")
);
// The results page requests each concept image at `<ConceptName>.png`, but the
// fixtures are stored as JPEG (photographic renders compress far smaller). Key
// the lookup by basename (no extension) so a `.png` request resolves the `.jpg`
// fixture; the mock then serves it with the image/jpeg content type.
const imageBytes = new Map(); // basename (no ext) -> Buffer
// images/live/ holds the three creatives of the first live bandit experiment.
for (const dir of [join(FIX, "images"), join(FIX, "images", "live")]) {
  for (const f of readdirSync(dir, { withFileTypes: true })) {
    if (f.isFile()) imageBytes.set(f.name.replace(/\.[^.]+$/, ""), readFileSync(join(dir, f.name)));
  }
}

// Campaign metadata for the input-form screen (mirrors the harvested run).
const CAMPAIGN = {
  brand: "Paul Reed Smith (PRS)",
  targetAudience:
    "Millennials who follow jam bands (e.g., Widespread Panic and Phish), " +
    "respond positively to nostalgic messages, and love surreal memes",
  targetProduct: "SE CE24 Electric Guitar",
  keySellingPoints:
    "The 85/15 S Humbucker pickups deliver a wide tonal range, from thick " +
    "humbucker tones to clear single-coil sounds, making the guitar suitable " +
    "for various genres.",
  targetSearchTrend: "Powerball",
  referenceImageUri: "gs://reference-images-jt-trend-trawler/prs.png",
};

const USER = "demo_user";

// Candidate trends for the 06 trend-pick screen (a plausible top-trends list).
const TREND_CANDIDATES = [
  "Powerball",
  "Phish tour dates",
  "Harvest moon",
  "Fantasy football rankings",
  "iPhone 18 release",
  "Hurricane season",
  "Taylor Swift",
  "College football scores",
  "Pumpkin spice latte",
  "Emmy nominations",
];

// Run history for the home "Recent runs" list and 05-runs.png. Each fixture's
// `minutesAgo` becomes an ADK `lastUpdateTime` (epoch seconds) relative to now,
// so the relative dates read the same on every regeneration.
const SESSIONS = JSON.parse(
  readFileSync(join(FIX, "sessions-list.json"), "utf8")
).map(({ minutesAgo, ...s }) => ({
  appName: "trend_scout",
  userId: USER,
  events: [],
  ...s,
  lastUpdateTime: (Date.now() - minutesAgo * 60_000) / 1000,
}));

// Bandit experiments (docs/bandit/contracts.md §5): synthetic, contract-shaped
// fixtures over three of the PRS creatives. Relative `*MinutesAgo` /
// `ttlMinutesLeft` become ISO timestamps at capture time so the "created" and
// TTL countdown text reads the same on every regeneration.
const hydrateExperiment = ({ createdMinutesAgo, updatedMinutesAgo, ttlMinutesLeft, ...e }) => ({
  ...e,
  createdAt: new Date(Date.now() - createdMinutesAgo * 60_000).toISOString(),
  updatedAt: new Date(Date.now() - updatedMinutesAgo * 60_000).toISOString(),
  ttlExpiresAt:
    ttlMinutesLeft == null ? null : new Date(Date.now() + ttlMinutesLeft * 60_000).toISOString(),
});
const EXPERIMENTS = JSON.parse(
  readFileSync(join(FIX, "experiments-list.json"), "utf8")
).experiments.map(hydrateExperiment);
const EXPERIMENT_DETAIL = hydrateExperiment(
  JSON.parse(readFileSync(join(FIX, "experiment-detail.json"), "utf8"))
);
const EXPERIMENT_METRICS = JSON.parse(
  readFileSync(join(FIX, "experiment-metrics.json"), "utf8")
);

// The FIRST LIVE bandit experiment (2026-10-02, 0693ea62bb7144ef), exported from
// the api as-is (only userId replaced): its final ExperimentSummary and the real
// ExperimentMetrics after 20 episodes x 40,000 rounds. Used by JOURNEY=experiments.
const LIVE_EXPERIMENT = JSON.parse(readFileSync(join(FIX, "live-experiment.json"), "utf8"));
const LIVE_METRICS = JSON.parse(readFileSync(join(FIX, "live-experiment-metrics.json"), "utf8"));
// What the mocks serve for LIVE_EXPERIMENT's id (set per journey frame).
let liveMock = { summary: LIVE_EXPERIMENT, metrics: LIVE_METRICS };

// The real .png artifact keys the run produced (from harvested state), so the
// results page's Artifacts list + gallery grid render authentically.
const ARTIFACT_NAMES = [
  ...(state._generated_artifact_keys ?? []),
  "research_report_with_citations.pdf",
  "creative_portfolio_gallery.html",
];

// An interactive run that stopped right after visual concepts: concepts exist,
// but no rendered images and no eval report.
function stoppedEarlyState() {
  const {
    _images_generated: _ig,
    _generated_artifact_keys: _gak,
    eval_report_gcs_uri: _er,
    ...rest
  } = state;
  return rest;
}

// Checkpoints 1 and 2 answered, then the root model's empty final turn.
function stoppedEarlyEvents() {
  const t0 = events[0]?.timestamp ?? 0;
  const answered = (name, offset) => ({
    id: `evt-${name}-answer`,
    invocationId: "inv-interactive",
    author: "user",
    timestamp: t0 + offset,
    content: {
      role: "user",
      parts: [{ functionResponse: { id: `fc-${name}`, name, response: { status: "approved" } } }],
    },
  });
  return [
    answered("review_research", 200),
    answered("review_ad_copies", 420),
    {
      id: "evt-empty-turn",
      invocationId: "inv-interactive",
      author: "interactive_creative",
      timestamp: t0 + 610,
      content: { role: "model", parts: [] },
    },
  ];
}

// ── Per-screen mock state (set before each navigation) ────────────────────────
let currentSession = { state: {}, events: [] };
let currentPoll = { status: "done", events: [], nextCursor: 0, state: {} };

const json = (route, body) =>
  route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

async function installMocks(page) {
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();

    // GCS proxy: images + eval report.
    if (path === "/api/gcs") {
      const p = url.searchParams.get("path") || "";
      if (p.endsWith("creative_eval_report.json")) return json(route, evalReport);
      if (p.endsWith(".png")) {
        const base = p.split("/").pop().replace(/\.[^.]+$/, "");
        const buf = imageBytes.get(base);
        if (buf) {
          return route.fulfill({ status: 200, contentType: "image/jpeg", body: buf });
        }
        return route.fulfill({ status: 404, body: "" });
      }
      // PDF / HTML links are not opened during capture.
      return route.fulfill({ status: 200, contentType: "application/octet-stream", body: "" });
    }

    // ADK proxy.
    if (path.startsWith("/api/adk/")) {
      const rest = path.slice("/api/adk/".length);

      // Async-run endpoints.
      if (rest.startsWith("runs/")) {
        if (method === "POST") {
          // startRun (runs/{app}) or resume (.../resume) — both just need ok.
          return json(route, { runId: "mock", status: "running" });
        }
        // GET poll (getRunStatus seed + pollRun loop).
        return json(route, currentPoll);
      }

      // Bandit experiments: create / list / detail / metrics / traffic / stop.
      if (rest === "experiments" || rest.startsWith("experiments/")) {
        const seg = rest.split("/"); // ["experiments", user?, id?, action?]
        if (method === "POST" && seg.length === 1) {
          return json(route, { experimentId: EXPERIMENT_DETAIL.experimentId, status: "deploying" });
        }
        if (method === "POST" && seg[3] === "traffic") {
          return json(route, { status: "running_traffic", execution: "mock" });
        }
        if (method === "POST" && seg[3] === "stop") return json(route, { status: "stopping" });
        if (seg.length === 2) return json(route, { experiments: EXPERIMENTS });
        if (seg[2] === LIVE_EXPERIMENT.experimentId) {
          return json(route, seg[3] === "metrics" ? liveMock.metrics : liveMock.summary);
        }
        if (seg[3] === "metrics") {
          return json(
            route,
            seg[2] === EXPERIMENT_METRICS.experimentId
              ? EXPERIMENT_METRICS
              : { ...EXPERIMENT_METRICS, experimentId: seg[2], episodes: 0, checkpoints: [] }
          );
        }
        if (seg.length === 3) {
          const found =
            seg[2] === EXPERIMENT_DETAIL.experimentId
              ? EXPERIMENT_DETAIL
              : EXPERIMENTS.find((e) => e.experimentId === seg[2]);
          if (found) return json(route, found);
          return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
        }
      }

      // Session CRUD.
      if (method === "POST" && /sessions$/.test(rest)) {
        return json(route, { id: "demo", appName: "creative_agent", userId: USER, state: {}, events: [] });
      }
      // listSessions (run history: home "Recent runs" + /runs).
      if (method === "GET" && /sessions$/.test(rest)) return json(route, SESSIONS);
      if (/sessions\/[^/]+\/artifacts$/.test(rest)) return json(route, ARTIFACT_NAMES);
      if (/sessions\/[^/]+\/artifacts\/.+/.test(rest)) return json(route, {});
      if (/sessions\/[^/]+$/.test(rest)) return json(route, currentSession);
    }

    return route.continue();
  });
}

// Kill entrance animations so captures are stable (fadeInUp starts at opacity-0
// with animation-fill forwards → zero duration jumps straight to the final state).
async function settle(page, { pinHeader = true } = {}) {
  await page.addStyleTag({
    content:
      "*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition-duration:0s!important;transition-delay:0s!important;}" +
      // fullPage capture re-paints sticky elements mid-page; pin the header
      // in-flow so it renders once at the true top with no content overlap.
      // (Viewport-only journey frames keep the sticky header.)
      (pinHeader ? "header{position:static!important;}" : "") +
      // hide the Next dev-tools badge when capturing against `npm run dev`
      "nextjs-portal{display:none!important;}",
  });
  await page.waitForTimeout(400);
}

async function newPage(context, { sessionId } = {}) {
  const page = await context.newPage();
  if (sessionId) {
    // Store the home form's kick-off message so the run page takes the
    // kick-off path (startRun is mocked) instead of view mode.
    await page.addInitScript(
      ([sid]) => {
        sessionStorage.setItem(
          `run:${sid}`,
          JSON.stringify({
            message:
              'Brand Name: "Paul Reed Smith (PRS)"\nTarget Audience: "..."\n' +
              'Target Product: "SE CE24 Electric Guitar"\ntarget_search_trend: "Powerball"',
          })
        );
      },
      [sessionId]
    );
  }
  await installMocks(page);
  return page;
}

async function shot(page, name, { fullPage = true } = {}) {
  await page.screenshot({ path: join(OUT, name), fullPage });
  console.log("  wrote", name);
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });

  // ── 1. Home / input form ────────────────────────────────────────────────
  {
    console.log("01-home-form");
    const page = await newPage(context);
    const qs = new URLSearchParams({
      agent: "creative_agent",
      brand: CAMPAIGN.brand,
      targetSearchTrend: CAMPAIGN.targetSearchTrend,
    });
    await page.goto(`${BASE}/?${qs}`, { waitUntil: "networkidle" });
    await page.waitForSelector("#referenceImage");
    await page.fill("#audience", CAMPAIGN.targetAudience);
    await page.fill("#product", CAMPAIGN.targetProduct);
    await page.fill("#selling-points", CAMPAIGN.keySellingPoints);
    await page.fill("#trend", CAMPAIGN.targetSearchTrend);
    await page.fill("#referenceImage", CAMPAIGN.referenceImageUri);
    await page.locator("#referenceImage").blur();
    await page.getByRole("heading", { name: "Recent runs" }).waitFor();
    await page.getByRole("link", { name: "Google" }).first().waitFor();
    await settle(page);
    await shot(page, "01-home-form.png");
    await page.close();
  }

  // ── 2. Run view (creative_agent, completed) ─────────────────────────────
  {
    console.log("02-run-creative");
    const sid = "run-creative-demo";
    currentSession = { id: sid, appName: "creative_agent", userId: USER, state, events: [] };
    currentPoll = { status: "done", events, nextCursor: events.length, state };
    const page = await newPage(context, { sessionId: sid });
    await page.goto(`${BASE}/run/${sid}?app=creative_agent&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("button", { name: "View results" }).first().waitFor();
    await settle(page);
    await shot(page, "02-run-creative.png");
    await page.close();
  }

  // ── 3. Results (creative_agent) with REAL generated images ──────────────
  {
    console.log("03-results-creative");
    const sid = "results-creative-demo";
    currentSession = { id: sid, appName: "creative_agent", userId: USER, state, events: [] };
    const page = await newPage(context);
    await page.goto(`${BASE}/results/${sid}?app=creative_agent&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    // Wait for a concept image to actually decode (real render, not placeholder).
    await page.waitForFunction(() => {
      const imgs = [...document.querySelectorAll("img")];
      return imgs.some((im) => im.naturalWidth > 0);
    });
    await settle(page);
    await shot(page, "03-results-creative.png");

    // ── 9. Proof-detail dialog for creative 1 (viewport: it's a modal) ──
    console.log("09-results-proof-detail");
    await page.locator("#proofs-heading").scrollIntoViewIfNeeded();
    await page.locator('section[aria-labelledby="proofs-heading"] li button').first().click();
    await page.getByRole("dialog").waitFor();
    await page.waitForFunction(() =>
      [...document.querySelectorAll('[role="dialog"] img')].some((im) => im.naturalWidth > 0)
    );
    await page.waitForTimeout(300);
    await shot(page, "09-results-proof-detail.png", { fullPage: false });
    await page.close();
  }

  // ── 10. Deploy panel on the results page (three creatives picked) ────────
  {
    console.log("10-deploy-panel");
    const sid = "results-creative-demo";
    currentSession = { id: sid, appName: "creative_agent", userId: USER, state, events: [] };
    const page = await newPage(context);
    await page.goto(`${BASE}/results/${sid}?app=creative_agent&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    const panel = page.locator('section[aria-labelledby="deploy-heading"]');
    await panel.waitFor();
    for (const i of [0, 1, 2]) await page.locator(`#deploy-creative-${i}`).check();
    await page.waitForFunction(() =>
      [...document.querySelectorAll('section[aria-labelledby="deploy-heading"] img')].every(
        (im) => im.naturalWidth > 0
      )
    );
    await settle(page);
    await scrollToLocator(page, panel, 24);
    await shot(page, "10-deploy-panel.png", { fullPage: false });
    await page.close();
  }

  // ── 11. Experiments list ────────────────────────────────────────────────
  {
    console.log("11-experiments");
    const page = await newPage(context);
    await page.goto(`${BASE}/experiments`, { waitUntil: "networkidle" });
    await page.getByRole("link", { name: "Segment-specific winners" }).first().waitFor();
    await settle(page);
    await shot(page, "11-experiments.png");
    await page.close();
  }

  // ── 12. Experiment detail with metrics (ready, after 20 episodes) ────────
  {
    console.log("12-experiment-detail");
    const page = await newPage(context);
    await page.goto(`${BASE}/experiments/${EXPERIMENT_DETAIL.experimentId}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("heading", { name: "Cumulative regret" }).waitFor();
    await page.waitForFunction(() =>
      [...document.querySelectorAll("img")].some((im) => im.naturalWidth > 0)
    );
    await settle(page);
    await shot(page, "12-experiment-detail.png");

    // ── 13. The Stop "ⓘ" help popover, opened by keyboard focus (viewport) ──
    console.log("13-experiment-help");
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.getByRole("button", { name: "About stop" }).focus();
    await page.getByText("Stopping deletes the live endpoint").waitFor();
    await page.waitForTimeout(250);
    await shot(page, "13-experiment-help.png", { fullPage: false });
    await page.close();
  }

  // ── 4. Interactive run paused at the Review Ad Copies checkpoint ─────────
  {
    console.log("04-run-interactive-review");
    const sid = "interactive-review-demo";
    const pauseEvent = {
      id: "evt-review-adcopies",
      invocationId: "inv-interactive",
      author: "interactive_creative",
      timestamp: 0,
      longRunningToolIds: ["fc-review-adcopies"],
      content: {
        role: "model",
        parts: [
          {
            functionCall: {
              id: "fc-review-adcopies",
              name: "review_ad_copies",
              args: {},
            },
          },
        ],
      },
    };
    // Paused at checkpoint 2: research + ad copy exist, nothing after it yet.
    // ReviewAdCopies reads ad_copy_critique; the campaign keys feed the brief.
    const {
      visual_direction: _vd,
      final_visual_concepts: _fvc,
      _images_generated: _ig,
      _generated_artifact_keys: _gak,
      eval_report_gcs_uri: _er,
      ...interactiveState
    } = state;
    // Checkpoint 1 was already answered (so "Review research" reads done).
    const answeredResearch = {
      id: "evt-review-research-answer",
      invocationId: "inv-interactive",
      author: "user",
      timestamp: (events[0]?.timestamp ?? 0) + 200,
      content: {
        role: "user",
        parts: [
          {
            functionResponse: {
              id: "fc-review-research",
              name: "review_research",
              response: { status: "approved", feedback: "" },
            },
          },
        ],
      },
    };
    pauseEvent.timestamp = (events[0]?.timestamp ?? 0) + 420;
    currentSession = { id: sid, appName: "interactive_creative", userId: USER, state: interactiveState, events: [] };
    currentPoll = {
      status: "done",
      events: [answeredResearch, pauseEvent],
      nextCursor: 2,
      state: interactiveState,
    };
    // No stored message: opened from history, so the page views the run.
    const page = await newPage(context);
    await page.goto(`${BASE}/run/${sid}?app=interactive_creative&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("heading", { name: "Review ad copies" }).waitFor();
    await page.getByRole("button", { name: /Approve/ }).first().waitFor();
    await settle(page);
    await shot(page, "04-run-interactive-review.png");
    await page.close();
  }

  // ── 7. Interactive run paused at checkpoint 1 (Review research report) ──
  {
    console.log("07-run-research-review");
    const sid = "interactive-research-demo";
    // Paused right after research: the report + its sources exist, nothing
    // downstream yet. ReviewResearch renders the cited report with numbered
    // superscript citations and a Sources list.
    const researchState = {
      brand: state.brand,
      target_product: state.target_product,
      target_audience: state.target_audience,
      key_selling_points: state.key_selling_points,
      target_search_trends: state.target_search_trends,
      combined_final_cited_report: state.combined_final_cited_report,
      sources: state.sources,
      research_report_gcs_uri: state.research_report_gcs_uri,
    };
    const pauseEvent = {
      id: "evt-review-research",
      invocationId: "inv-interactive",
      author: "interactive_creative",
      timestamp: (events[0]?.timestamp ?? 0) + 180,
      longRunningToolIds: ["fc-review-research"],
      content: {
        role: "model",
        parts: [{ functionCall: { id: "fc-review-research", name: "review_research", args: {} } }],
      },
    };
    currentSession = { id: sid, appName: "interactive_creative", userId: USER, state: researchState, events: [] };
    currentPoll = { status: "done", events: [pauseEvent], nextCursor: 1, state: researchState };
    const page = await newPage(context);
    await page.goto(`${BASE}/run/${sid}?app=interactive_creative&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("heading", { name: "Review research report" }).waitFor();
    await page.getByRole("heading", { name: /Sources/ }).waitFor();
    await settle(page);
    await shot(page, "07-run-research-review.png");
    await page.close();
  }

  // ── 8. Interactive run that stopped early (after visual concepts) ───────
  {
    console.log("08-run-stopped-early");
    const sid = "interactive-stopped-demo";
    // The segment ended "done" right after visual concepts: no checkpoint-3
    // pause, no images, no eval report — so the page offers "Continue run".
    const stoppedState = stoppedEarlyState();
    currentSession = { id: sid, appName: "interactive_creative", userId: USER, state: stoppedState, events: [] };
    currentPoll = {
      status: "done",
      events: stoppedEarlyEvents(),
      nextCursor: 3,
      state: stoppedState,
    };
    const page = await newPage(context);
    await page.goto(`${BASE}/run/${sid}?app=interactive_creative&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("button", { name: "Continue run" }).waitFor();
    await settle(page);
    await shot(page, "08-run-stopped-early.png");
    await page.close();
  }

  // ── 6. trend_scout paused at the opt-in trend pick (review_trends) ──────
  {
    console.log("06-run-trend-pick");
    const sid = "trend-pick-demo";
    const pickState = {
      brand: state.brand,
      target_product: state.target_product,
      target_audience: state.target_audience,
      key_selling_points: state.key_selling_points,
      target_search_trends: { target_search_trends: [] },
      interactive_trend_pick: true,
      raw_gtrends: TREND_CANDIDATES,
    };
    const gatherEvent = {
      id: "evt-gather",
      invocationId: "inv-scout",
      author: "trend_scout",
      timestamp: events[0]?.timestamp ?? 0,
      actions: { stateDelta: { raw_gtrends: TREND_CANDIDATES } },
      content: { role: "model", parts: [{ text: "Gathered today's top 25 search trends." }] },
    };
    const pauseEvent = {
      id: "evt-review-trends",
      invocationId: "inv-scout",
      author: "trend_scout",
      timestamp: (events[0]?.timestamp ?? 0) + 40,
      longRunningToolIds: ["fc-review-trends"],
      content: {
        role: "model",
        parts: [{ functionCall: { id: "fc-review-trends", name: "review_trends", args: {} } }],
      },
    };
    currentSession = { id: sid, appName: "trend_scout", userId: USER, state: pickState, events: [] };
    currentPoll = { status: "done", events: [gatherEvent, pauseEvent], nextCursor: 2, state: pickState };
    const page = await newPage(context);
    await page.goto(`${BASE}/run/${sid}?app=trend_scout&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("heading", { name: "Pick your trends" }).waitFor();
    // Pick two trends so the confirm action and shortcut hint show.
    await page.getByRole("button", { name: TREND_CANDIDATES[0] }).click();
    await page.getByRole("button", { name: TREND_CANDIDATES[3] }).click();
    await settle(page);
    await shot(page, "06-run-trend-pick.png");
    await page.close();
  }

  // ── 5. Run history ──────────────────────────────────────────────────────
  {
    console.log("05-runs");
    const page = await newPage(context);
    await page.goto(`${BASE}/runs`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /Duplicate brief/ }).first().waitFor();
    await settle(page);
    await shot(page, "05-runs.png");
    await page.close();
  }

  await context.close();
  await browser.close();
  console.log("done →", OUT);
}

// ── User-journey mode (JOURNEY=1) ─────────────────────────────────────────────
// Viewport frames of one coherent interactive_creative story. Each step is its
// own mocked state (derived from the same harvested run), not one live session.

const JOURNEY_OUT = process.env.JOURNEY_OUT ?? "/tmp/tt-journey-frames";
const T0 = events[0]?.timestamp ?? 0;

// The current-stage panel's description while research runs.
const STAGE_RESEARCH_TEXT = "Searching the web for context on the trend and the campaign.";

const pauseAt = (name, offset) => ({
  id: `evt-${name}`,
  invocationId: "inv-interactive",
  author: "interactive_creative",
  timestamp: T0 + offset,
  longRunningToolIds: [`fc-${name}`],
  content: { role: "model", parts: [{ functionCall: { id: `fc-${name}`, name, args: {} } }] },
});

const answeredAt = (name, offset) => ({
  id: `evt-${name}-answer`,
  invocationId: "inv-interactive",
  author: "user",
  timestamp: T0 + offset,
  content: {
    role: "user",
    parts: [{ functionResponse: { id: `fc-${name}`, name, response: { status: "approved" } } }],
  },
});

// State as it stands once the given keys exist (campaign keys always present).
const CAMPAIGN_KEYS = [
  "reference_image_uri",
  "brand",
  "target_product",
  "target_audience",
  "key_selling_points",
  "target_search_trends",
];
const stateWith = (...keys) =>
  Object.fromEntries(
    [...CAMPAIGN_KEYS, ...keys].filter((k) => k in state).map((k) => [k, state[k]])
  );

const RESEARCH_KEYS = ["combined_final_cited_report", "sources", "research_report_gcs_uri"];
const VISUAL_KEYS = ["visual_direction", "final_visual_concepts"];

// Scroll so `locator` sits just below the sticky header.
async function scrollToLocator(page, locator, offset = 80) {
  await locator.evaluate(
    (el, off) => window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - off }),
    offset
  );
  await page.waitForTimeout(200);
}

// Box of one or more locators (union), padded, intersected with `clip` (a
// scroll container) and the viewport. Null (logged) if nothing is visible.
// `place` (above|below|right|left) is an optional callout-placement hint for the GIF.
async function highlightBox(page, { target, label, clip, pad = 8, place }) {
  const targets = Array.isArray(target) ? target : [target];
  const boxes = [];
  for (const t of targets) {
    try {
      await t.first().waitFor({ state: "visible", timeout: 3000 });
      const b = await t.first().boundingBox();
      if (b) boxes.push(b);
    } catch {
      // not found: logged below if no target produced a box
    }
  }
  if (!boxes.length) {
    console.warn(`  ! highlight not found: "${label}"`);
    return null;
  }
  let x0 = Math.min(...boxes.map((b) => b.x)) - pad;
  let y0 = Math.min(...boxes.map((b) => b.y)) - pad;
  let x1 = Math.max(...boxes.map((b) => b.x + b.width)) + pad;
  let y1 = Math.max(...boxes.map((b) => b.y + b.height)) + pad;
  if (clip) {
    const c = await clip.first().boundingBox();
    if (c) {
      x0 = Math.max(x0, c.x - pad);
      y0 = Math.max(y0, c.y - pad);
      x1 = Math.min(x1, c.x + c.width + pad);
      y1 = Math.min(y1, c.y + c.height + pad);
    }
  }
  const vp = page.viewportSize();
  x0 = Math.max(x0, 2);
  y0 = Math.max(y0, 2);
  x1 = Math.min(x1, vp.width - 2);
  y1 = Math.min(y1, vp.height - 2);
  if (x1 - x0 < 8 || y1 - y0 < 8) {
    console.warn(`  ! highlight off-screen: "${label}"`);
    return null;
  }
  const r = (v) => Math.round(v);
  return { x: r(x0), y: r(y0), w: r(x1 - x0), h: r(y1 - y0), label, ...(place && { place }) };
}

// A fresh frames directory plus `frame(page, spec)`, which captures one GIF frame
// (viewport screenshot + manifest entry with the resolved highlight boxes).
function frameRecorder(outDir) {
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  const frames = [];
  const frame = async (page, { phase, title, caption, hold_ms = 3200, highlights = [] }) => {
    const step = frames.length + 1;
    const file = `${String(step).padStart(2, "0")}.png`;
    const boxes = [];
    for (const h of highlights.slice(0, 2)) {
      const b = await highlightBox(page, h);
      if (b) boxes.push(b);
    }
    await page.screenshot({ path: join(outDir, file) });
    frames.push({ file, step, phase, title, caption, hold_ms, highlights: boxes });
    console.log("  frame", file, "-", title, `(${boxes.length} highlights)`);
  };
  return { frames, frame };
}

async function journey() {
  const { frames, frame } = frameRecorder(JOURNEY_OUT);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  const review = (page) => page.locator('section[aria-label="Review"]');
  const spine = (page) => page.locator('nav[aria-label="Run stages"]');
  // The scrollable list/report box inside the review panel.
  const reviewScroller = (page) => review(page).locator("div.overflow-y-auto").first();
  // Open an interactive run in view mode with the given poll payload.
  const openRun = async (sid, { status = "done", evts, st }) => {
    currentSession = { id: sid, appName: "interactive_creative", userId: USER, state: st, events: [] };
    currentPoll = { status, events: evts, nextCursor: evts.length, state: st };
    const page = await newPage(context);
    await page.goto(`${BASE}/run/${sid}?app=interactive_creative&userId=${USER}`, {
      waitUntil: "domcontentloaded",
    });
    return page;
  };

  // a. Home: pick the interactive agent, fill in the brief.
  {
    const page = await newPage(context);
    const qs = new URLSearchParams({ brand: CAMPAIGN.brand });
    await page.goto(`${BASE}/?${qs}`, { waitUntil: "networkidle" });
    await page.getByText("Creative run with reviews", { exact: true }).first().click();
    await page.fill("#audience", CAMPAIGN.targetAudience);
    await page.fill("#product", CAMPAIGN.targetProduct);
    await page.fill("#selling-points", CAMPAIGN.keySellingPoints);
    await page.fill("#trend", CAMPAIGN.targetSearchTrend);
    await page.fill("#referenceImage", CAMPAIGN.referenceImageUri);
    await page.locator("#referenceImage").blur();
    await page.getByRole("link", { name: "Google" }).first().waitFor();
    await settle(page, { pinHeader: false });
    await frame(page, {
      phase: "Brief",
      title: "Start a run",
      caption: "Choose the creative run with reviews, then describe the brand, product, audience and trend.",
      hold_ms: 3600,
      highlights: [
        {
          target: page.locator("label", { has: page.locator('input[value="interactive_creative"]') }),
          label: "Pick the agent",
          pad: 4,
        },
        {
          target: [page.getByText("Brand name", { exact: true }), page.locator("#referenceImage")],
          label: "Describe the campaign",
        },
      ],
    });
    await page.close();
  }

  // b. Research in progress (the poll keeps reporting "running").
  {
    const page = await openRun("journey-research", {
      status: "running",
      evts: events.slice(0, 4),
      st: stateWith(),
    });
    await page.getByText(STAGE_RESEARCH_TEXT).first().waitFor();
    await settle(page, { pinHeader: false });
    await frame(page, {
      phase: "Research",
      title: "Agents research the trend",
      caption: "Agents search the web for the trend and the brand; the run keeps going if you close the tab.",
      hold_ms: 3200,
      highlights: [
        { target: spine(page), label: "Live progress through each stage" },
        {
          target: page.locator("section[aria-live]").filter({ hasText: STAGE_RESEARCH_TEXT }),
          label: "What's running now",
          pad: 4,
        },
      ],
    });
    await page.close();
  }

  // c. Checkpoint 1: review the research report, edit it, approve.
  {
    const page = await openRun("journey-review-research", {
      evts: [pauseAt("review_research", 180)],
      st: stateWith(...RESEARCH_KEYS),
    });
    await page.getByRole("heading", { name: "Review research report" }).waitFor();
    await page.getByRole("heading", { name: /Sources/ }).waitFor();
    await settle(page, { pinHeader: false });
    await scrollToLocator(page, review(page));
    const reportBox = reviewScroller(page);
    await frame(page, {
      phase: "Reviews",
      title: "Review the research",
      caption: "The run pauses so you can check the cited research before any creative is written.",
      hold_ms: 3800,
      highlights: [
        { target: reportBox, label: "Review the research", pad: 4 },
        {
          target: reportBox.locator("sup").first(),
          label: "Cited sources",
          clip: reportBox,
          pad: 6,
          place: "right",
        },
      ],
    });

    await page.getByRole("button", { name: "Edit", exact: true }).click();
    const ta = review(page).locator("textarea").first();
    const original = await ta.inputValue();
    // Insert a visible sentence after the report's first paragraph.
    const added =
      "Editor's note: lean into the jackpot daydream. The SE CE24 is the win you can actually hold.";
    const cut = original.indexOf("\n\n", original.indexOf("\n") + 1);
    const at = cut > 0 ? cut : 0;
    await ta.fill(`${original.slice(0, at)}\n\n${added}${original.slice(at)}`);
    await ta.evaluate((el) => {
      el.scrollTop = 0;
    });
    await page.waitForTimeout(200);
    await frame(page, {
      phase: "Reviews",
      title: "Steer it with an edit",
      caption: "Anything you add or cut here shapes the ad copy and visuals that follow.",
      hold_ms: 3600,
      highlights: [{ target: ta, label: "Edit the report to steer the creative", pad: 4 }],
    });

    // Approve: bring the button into view with the edited report above it.
    const approve = page.getByRole("button", { name: /Approve/ }).first();
    await scrollToLocator(page, approve, 620);
    await frame(page, {
      phase: "Reviews",
      title: "Approve and continue",
      caption: "Approving resumes the run, which moves on to writing ad copy.",
      hold_ms: 2800,
      highlights: [{ target: approve, label: "Approve to continue", pad: 6 }],
    });
    await page.close();
  }

  // d. Checkpoint 2: review ad copies.
  {
    const page = await openRun("journey-review-adcopies", {
      evts: [answeredAt("review_research", 200), pauseAt("review_ad_copies", 420)],
      st: stateWith(...RESEARCH_KEYS, "ad_copy_critique"),
    });
    await page.getByRole("heading", { name: "Review ad copies" }).waitFor();
    await settle(page, { pinHeader: false });
    await scrollToLocator(page, review(page));
    await frame(page, {
      phase: "Reviews",
      title: "Review the ad copy",
      caption: "Each draft comes with its call to action, caption and how it ties to the trend.",
      hold_ms: 3400,
      highlights: [
        {
          target: review(page).locator("dl").first(),
          label: "Review ad copy",
          clip: reviewScroller(page),
          pad: 4,
        },
      ],
    });
    await page.close();
  }

  // e. Checkpoint 3: review visual concepts.
  {
    const page = await openRun("journey-review-visuals", {
      evts: [
        answeredAt("review_research", 200),
        answeredAt("review_ad_copies", 440),
        pauseAt("review_visual_concepts", 620),
      ],
      st: stateWith(...RESEARCH_KEYS, "ad_copy_critique", ...VISUAL_KEYS),
    });
    await page.getByRole("heading", { name: "Review visual concepts" }).waitFor();
    await settle(page, { pinHeader: false });
    await scrollToLocator(page, review(page));
    await frame(page, {
      phase: "Reviews",
      title: "Shape the visuals",
      caption: "Change a prompt, aspect ratio or style before any image is rendered.",
      hold_ms: 3400,
      highlights: [
        {
          target: reviewScroller(page).locator("> div").first(),
          label: "Tweak visual concepts before images render",
          clip: reviewScroller(page),
          pad: 4,
        },
      ],
    });
    await page.close();
  }

  // f. Run complete: all three reviews answered, every stage key present.
  {
    const finalTurn = events.at(-1);
    const page = await openRun("journey-complete", {
      evts: [
        answeredAt("review_research", 200),
        answeredAt("review_ad_copies", 440),
        answeredAt("review_visual_concepts", 640),
        { ...finalTurn, author: "interactive_creative", timestamp: T0 + 900 },
      ],
      st: state,
    });
    const viewResults = page.getByRole("button", { name: "View results" }).first();
    await viewResults.waitFor();
    await settle(page, { pinHeader: false });
    await frame(page, {
      phase: "Images & eval",
      title: "Images rendered and judged",
      caption: "Images are generated and every creative is scored by an AI judge, then saved.",
      hold_ms: 3200,
      highlights: [
        { target: spine(page), label: "Every stage done" },
        { target: viewResults, label: "Open the results", pad: 6 },
      ],
    });
    await page.close();
  }

  // g. Results: contact sheet, proof detail, research report.
  {
    const sid = "journey-results";
    currentSession = { id: sid, appName: "interactive_creative", userId: USER, state, events: [] };
    const page = await newPage(context);
    await page.goto(`${BASE}/results/${sid}?app=interactive_creative&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.waitForFunction(
      () => [...document.querySelectorAll("img")].filter((im) => im.naturalWidth > 0).length >= 4
    );
    await settle(page, { pinHeader: false });
    const firstProof = page.locator('section[aria-labelledby="proofs-heading"] li').first();
    await frame(page, {
      phase: "Results",
      title: "Compare the creatives",
      caption: "The contact sheet shows every image with its headline and a pass or fail per score.",
      hold_ms: 4000,
      highlights: [
        {
          target: page
            .getByText(/ad copies and .* visuals pass/)
            .first()
            .locator('xpath=ancestor::div[contains(@class,"rounded-lg")][1]'),
          label: "Pass rate at a glance",
          pad: 3,
        },
        {
          target: firstProof.locator("div.border-t").first(),
          label: "Scores per creative",
          pad: 6,
          place: "below",
        },
      ],
    });

    await page.locator('section[aria-labelledby="proofs-heading"] li button').first().click();
    await page.getByRole("dialog").waitFor();
    await page.waitForFunction(() =>
      [...document.querySelectorAll('[role="dialog"] img')].some((im) => im.naturalWidth > 0)
    );
    await page.waitForTimeout(300);
    await frame(page, {
      phase: "Results",
      title: "Open a proof",
      caption: "Each creative breaks its score down by dimension, with strengths and fixes.",
      hold_ms: 3600,
      highlights: [
        {
          target: page.getByRole("dialog").locator("ul").filter({ hasText: "Strategy fit" }).first(),
          label: "Why it scored that way",
          pad: 6,
          place: "below",
        },
      ],
    });
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "detached" });

    const trigger = page.getByRole("button", { name: "Research report" });
    await trigger.click();
    await scrollToLocator(page, trigger, 90);
    const summaryHeading = page.getByRole("heading", { name: "Executive Summary" }).first();
    await frame(page, {
      phase: "Results",
      title: "Read the research",
      caption: "The research behind the creatives reads like a brief, with numbered citations.",
      hold_ms: 3200,
      highlights: [
        {
          target: [summaryHeading, summaryHeading.locator("xpath=following-sibling::ul[1]")],
          label: "Readable report with sources",
          place: "right",
        },
      ],
    });
    await page.close();
  }

  // h. Run history.
  {
    const page = await newPage(context);
    await page.goto(`${BASE}/runs`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /Duplicate brief/ }).first().waitFor();
    await settle(page, { pinHeader: false });
    await frame(page, {
      phase: "History",
      title: "Find past runs",
      caption: "Every run is kept; duplicate a brief to start a new run from an old one.",
      hold_ms: 3600,
      highlights: [
        {
          target: page.locator('section[aria-label="Run history"]'),
          label: "Every run, with Duplicate brief",
          pad: 4,
          place: "below",
        },
      ],
    });
    await page.close();
  }

  writeFileSync(join(JOURNEY_OUT, "journey-frames.json"), JSON.stringify(frames, null, 2));
  await context.close();
  await browser.close();
  console.log("done →", JOURNEY_OUT);
}

// ── Experiments journey (JOURNEY=experiments) ─────────────────────────────────
// The first LIVE bandit experiment (live-experiment*.json + images/live/), told as
// deploy → endpoint → traffic → results → stop. Every chart frame shows the REAL
// final metrics (20 episodes) and says so; the deploying / ready / running frames
// show only the status and controls (no partial charts are invented). The Deploy
// panel frames reuse the PRS results fixtures (the deploy UI, not the live arms).
// Writes {phases, frames} to EXPERIMENTS_JOURNEY_OUT for build-journey-gif.py:
//   JOURNEY=experiments SCREENSHOT_BASE_URL=http://localhost:3600 npm run screenshots
//   uv run --no-project --with pillow python scripts/build-journey-gif.py \
//     --frames /tmp/tt-experiments-journey-frames --out ../docs/screenshots/experiments-journey.gif

const EXPERIMENTS_JOURNEY_OUT =
  process.env.EXPERIMENTS_JOURNEY_OUT ?? "/tmp/tt-experiments-journey-frames";
const EXPERIMENT_PHASES = ["Deploy", "Endpoint", "Traffic", "Results", "Stop"];

// The live summary as it read `minutesIn` minutes after the deploy request
// (measured: ready ~11 min in, traffic done ~47 min in; the endpoint TTL was 120 min).
function liveSummaryAt(minutesIn, overrides) {
  const created = Date.now() - minutesIn * 60_000;
  return {
    ...LIVE_EXPERIMENT,
    createdAt: new Date(created).toISOString(),
    updatedAt: new Date().toISOString(),
    ttlExpiresAt: new Date(created + 120 * 60_000).toISOString(),
    ...overrides,
  };
}
const NO_METRICS = { ...LIVE_METRICS, episodes: 0, checkpoints: [], curves: {}, totals: {} };

async function journeyExperiments() {
  const { frames, frame } = frameRecorder(EXPERIMENTS_JOURNEY_OUT);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
  });
  const id = LIVE_EXPERIMENT.experimentId;
  const status = (page) => page.locator("h1").locator("xpath=../..").locator("> div").last();
  const chart = (page, title) =>
    page.locator("section.bg-card").filter({ has: page.getByRole("heading", { name: title, exact: true }) });
  const openExperiment = async (summary, metrics) => {
    liveMock = { summary, metrics };
    const page = await newPage(context);
    await page.goto(`${BASE}/experiments/${id}`, { waitUntil: "networkidle" });
    await page.locator("h1").waitFor();
    await page.waitForFunction(
      () => [...document.querySelectorAll("img")].filter((im) => im.naturalWidth > 0).length >= 3
    );
    await settle(page, { pinHeader: false });
    return page;
  };

  // 1–2. Results page → Deploy panel: pick three creatives, then the options.
  {
    const sid = "results-creative-demo";
    currentSession = { id: sid, appName: "creative_agent", userId: USER, state, events: [] };
    const page = await newPage(context);
    await page.goto(`${BASE}/results/${sid}?app=creative_agent&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    const panel = page.locator('section[aria-labelledby="deploy-heading"]');
    await panel.waitFor();
    for (const i of [0, 1, 2]) await page.locator(`#deploy-creative-${i}`).check();
    await page.waitForFunction(() =>
      [...document.querySelectorAll('section[aria-labelledby="deploy-heading"] img')].every(
        (im) => im.naturalWidth > 0
      )
    );
    await settle(page, { pinHeader: false });
    await scrollToLocator(page, panel, 80);
    await frame(page, {
      phase: "Deploy",
      title: "Pick creatives to test",
      caption: "From a finished run's results, tick 2–4 scored creatives to become the arms of a bandit.",
      hold_ms: 3400,
      highlights: [{ target: panel.locator("fieldset"), label: "Pick 2–4 creatives to test", pad: 6 }],
    });

    const deployButton = panel.getByRole("button", { name: "Deploy 3 creatives" });
    await frame(page, {
      phase: "Deploy",
      title: "Choose the simulation, then deploy",
      caption: "This run: segment-specific winners, demo click rates, click reward, a 120-minute endpoint lifetime.",
      hold_ms: 3800,
      highlights: [
        {
          target: panel.locator("div.grid").filter({ has: page.locator("#deploy-ttl") }),
          label: "Scenario, click rates, reward, lifetime",
          pad: 6,
          place: "above",
        },
        { target: deployButton, label: "Deploy 3 creatives", pad: 6, place: "right" },
      ],
    });
    await page.close();
  }

  // 3. Deploying the endpoint.
  {
    const page = await openExperiment(liveSummaryAt(4, { status: "deploying" }), NO_METRICS);
    await page.getByText("Deploying the endpoint").waitFor();
    await frame(page, {
      phase: "Endpoint",
      title: "The endpoint deploys",
      caption: "A JAX linear Thompson sampling bandit goes onto an Agent Platform endpoint: about 11 minutes here.",
      hold_ms: 3400,
      highlights: [
        { target: status(page), label: "Deploying", pad: 6, place: "left" },
        { target: page.getByText("Deploying the endpoint"), label: "You can leave the page", pad: 6, place: "below" },
      ],
    });
    await page.close();
  }

  // 4. Ready: start synthetic traffic.
  {
    const page = await openExperiment(liveSummaryAt(11, { status: "ready" }), NO_METRICS);
    const start = page.getByRole("button", { name: "Start traffic", exact: true });
    await start.waitFor();
    await frame(page, {
      phase: "Endpoint",
      title: "Ready: start synthetic readers",
      caption: "This run used 20 episodes of 40,000 simulated readers each against the live endpoint.",
      hold_ms: 3400,
      highlights: [
        {
          target: [page.locator('label[for="traffic-episodes"]'), start],
          label: "Start synthetic readers",
          pad: 10,
          place: "below",
        },
      ],
    });
    await page.close();
  }

  // 5. Traffic running (status + progress only; no charts until an episode finishes).
  {
    const summary = liveSummaryAt(12, {
      status: "running_traffic",
      progress: { episodesDone: 0, episodesTotal: 20 },
    });
    const page = await openExperiment(summary, NO_METRICS);
    const note = page.getByText(/Simulating readers/);
    await note.waitFor();
    await frame(page, {
      phase: "Traffic",
      title: "Synthetic readers arrive",
      caption: "A Cloud Run job sends readers to the endpoint; five baselines replay on the same readers (~23 min here).",
      hold_ms: 3600,
      highlights: [
        { target: status(page), label: "Running traffic", pad: 6, place: "left" },
        { target: note, label: "Episode 1 of 20", pad: 6, place: "below" },
      ],
    });
    await page.close();
  }

  // 6–9. Results after all 20 episodes (the real final metrics).
  {
    const page = await openExperiment(liveSummaryAt(47, { status: "ready" }), LIVE_METRICS);
    const avg = chart(page, "Cumulative average reward against the optimum");
    await avg.waitFor();
    await scrollToLocator(page, avg, 96);
    await frame(page, {
      phase: "Results",
      title: "Reward vs the oracle, after 20 episodes",
      caption: "Linear TS on the endpoint: 1,760 clicks per episode vs 1,602–1,667 for baselines (oracle 2,028).",
      hold_ms: 4000,
      highlights: [
        { target: avg, label: "Reward vs the oracle", pad: 4, place: "below" },
        {
          target: avg.locator("svg g").filter({ has: page.locator("text", { hasText: /^Linear TS$/ }) }),
          label: "Your endpoint closes in on the oracle",
          pad: 6,
          place: "right",
        },
      ],
    });

    const regret = chart(page, "Cumulative regret");
    await frame(page, {
      phase: "Results",
      title: "Cumulative regret, after 20 episodes",
      caption: "Clicks lost against the oracle, mean of 20 episodes: Linear TS 257, baselines 335–403.",
      hold_ms: 4000,
      highlights: [{ target: regret, label: "Lowest regret: 257 vs 335–403", pad: 4, place: "below" }],
    });

    const segments = chart(page, "Winners by reader segment");
    await scrollToLocator(page, segments, 100);
    await frame(page, {
      phase: "Results",
      title: "Winners by reader segment",
      caption: "Each creative wins a segment; in the two niche segments Linear TS finds the winner 50–61% of the time, the best baseline 33%.",
      hold_ms: 4200,
      highlights: [
        { target: segments, label: "Different creatives win for different readers", pad: 4, place: "above" },
      ],
    });

    const share = chart(page, "Where the endpoint sends traffic");
    await scrollToLocator(page, share, 140);
    await frame(page, {
      phase: "Results",
      title: "Where the endpoint sends traffic",
      caption: "The top segment winner ends at 44% of impressions; the other two keep 25–31% for their readers.",
      hold_ms: 4000,
      highlights: [
        { target: share, label: "Traffic shifts toward each segment's winner", pad: 4, place: "above" },
      ],
    });

    // 10. Stop, with its help open (keyboard focus on the ⓘ).
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(200);
    const about = page.getByRole("button", { name: "About stop" });
    await about.focus();
    const help = page.getByText("Stopping deletes the live endpoint");
    await help.waitFor();
    await page.waitForTimeout(250);
    await frame(page, {
      phase: "Stop",
      title: "Stop when you're done",
      caption: "Stop deletes the endpoint and its model (about 2 seconds); results and charts stay on the page.",
      hold_ms: 4200,
      highlights: [
        {
          target: [page.getByRole("button", { name: "Stop", exact: true }), help],
          label: "Stop deletes the endpoint; results are kept",
          pad: 8,
          place: "below",
        },
      ],
    });
    await page.close();
  }

  writeFileSync(
    join(EXPERIMENTS_JOURNEY_OUT, "journey-frames.json"),
    JSON.stringify({ phases: EXPERIMENT_PHASES, frames }, null, 2)
  );
  await context.close();
  await browser.close();
  console.log("done →", EXPERIMENTS_JOURNEY_OUT);
}

const MODE = process.env.JOURNEY;
(MODE === "experiments" ? journeyExperiments : MODE ? journey : main)().catch((err) => {
  console.error(err);
  process.exit(1);
});
