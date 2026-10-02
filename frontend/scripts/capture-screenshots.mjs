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

import { chromium } from "playwright";
import { readFileSync, readdirSync } from "node:fs";
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
for (const f of readdirSync(join(FIX, "images"))) {
  imageBytes.set(f.replace(/\.[^.]+$/, ""), readFileSync(join(FIX, "images", f)));
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
async function settle(page) {
  await page.addStyleTag({
    content:
      "*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition-duration:0s!important;transition-delay:0s!important;}" +
      // fullPage capture re-paints sticky elements mid-page; pin the header
      // in-flow so it renders once at the true top with no content overlap.
      "header{position:static!important;}" +
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

async function shot(page, name) {
  await page.screenshot({ path: join(OUT, name), fullPage: true });
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

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
