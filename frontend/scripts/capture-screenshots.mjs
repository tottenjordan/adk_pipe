// Regenerate the four docs/screenshots/*.png from committed fixtures.
//
// These captures are DETERMINISTIC and need no GCP credentials or quota: every
// backend call (`/api/adk/**` and `/api/gcs?**`) is route-mocked from the
// fixtures in ./screenshot-fixtures/, which were harvested from ONE real
// `creative_agent` run of the Paul Reed Smith / SE CE24 / Powerball campaign.
// So 03-results-creative.png shows the ACTUAL generated concept images (the
// downscaled real renders in ./screenshot-fixtures/images/), and all four
// screens reflect the same campaign.
//
// Usage:
//   cd frontend
//   npm run dev                # in one terminal (serves localhost:3000)
//   npm run screenshots        # in another
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

// The real .png artifact keys the run produced (from harvested state), so the
// results page's Artifacts list + gallery grid render authentically.
const ARTIFACT_NAMES = [
  ...(state._generated_artifact_keys ?? []),
  "research_report_with_citations.pdf",
  "creative_portfolio_gallery.html",
];

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
      "header{position:static!important;}",
  });
  await page.waitForTimeout(400);
}

async function newPage(context, { sessionId } = {}) {
  const page = await context.newPage();
  if (sessionId) {
    // The run page refuses to render without the stored kickoff message.
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
    await page.getByRole("button", { name: "View Results" }).first().waitFor();
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
    // ReviewAdCopies reads ad_copy_critique; keep the campaign keys for the sidebar.
    const interactiveState = { ...state };
    currentSession = { id: sid, appName: "interactive_creative", userId: USER, state: interactiveState, events: [] };
    currentPoll = { status: "done", events: [pauseEvent], nextCursor: 1, state: interactiveState };
    const page = await newPage(context, { sessionId: sid });
    await page.goto(`${BASE}/run/${sid}?app=interactive_creative&userId=${USER}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("heading", { name: "Review Ad Copies" }).waitFor();
    await page.getByRole("button", { name: /Approve/ }).first().waitFor();
    await settle(page);
    await shot(page, "04-run-interactive-review.png");
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
