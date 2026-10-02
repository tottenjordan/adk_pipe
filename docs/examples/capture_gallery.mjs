// Capture the creative_agent HTML gallery (creative_portfolio_gallery.html) for
// docs/examples/: the overview, one creative hovered (the four hover facts), and
// the click-to-enlarge lightbox.
//
// Input: the gallery copy rewritten to local images by `capture_examples.py prep`.
// Output: raw PNGs in OUT (optimize them with `capture_examples.py optimize`).
//
// Playwright comes from the frontend's node_modules:
//   NODE_PATH=frontend/node_modules node docs/examples/capture_gallery.mjs \
//     /tmp/ex/creative_output/local/gallery.html /tmp/ex/raw
// (Chromium on a bare Linux box may need libxkbcommon: see docs/screenshots/README.md.)

import { createRequire } from "node:module";
import { join, resolve } from "node:path";

const require = createRequire(join(process.env.NODE_PATH ?? ".", "noop.js"));
const { chromium } = require("playwright");

const [htmlPath, out] = process.argv.slice(2);
const url = `file://${resolve(htmlPath)}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1200, height: 900 },
  deviceScaleFactor: 2,
});
await page.goto(url, { waitUntil: "load" });
await page.waitForFunction(() =>
  [...document.querySelectorAll(".gallery-item img")].every(
    (img) => img.complete && img.naturalWidth > 0
  )
);

// 1. Overview: header + the first row of creatives.
await page.screenshot({ path: join(out, "gallery-overview.png") });

// 2. Full page (visual concepts / ad copy sections below the gallery).
await page.screenshot({ path: join(out, "gallery-full-page.png"), fullPage: true });

// 3. One creative hovered: the four hover facts overlay the image.
const item = page.locator(".gallery-item").first();
await item.scrollIntoViewIfNeeded();
await item.hover();
await page.waitForTimeout(600); // CSS transition
await item.screenshot({ path: join(out, "gallery-hover-facts.png") });

// 4. Lightbox: click the image, wait for the overlay's .visible class.
await page.mouse.move(0, 0);
await item.locator("img").click();
await page.waitForSelector("#lightbox.visible");
await page.waitForFunction(() => {
  const img = document.getElementById("lightbox-img");
  return img.complete && img.naturalWidth > 0;
});
await page.waitForTimeout(600);
await page.screenshot({ path: join(out, "gallery-lightbox.png") });

await browser.close();
