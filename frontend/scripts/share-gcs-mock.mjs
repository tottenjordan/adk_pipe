// Server-side GCS mock for the public share captures (21+ in capture-screenshots.mjs).
//
// The /s/<token> page and its /s/<token>/img/<n> route read the snapshot and images
// from Cloud Storage ON THE SERVER (lib/share-snapshot.ts: metadata-server token +
// storage.googleapis.com JSON API), so Playwright's page.route() can't mock them.
// Preload this module into the Next server instead; it wraps global fetch and answers
// only those two hosts from screenshot-fixtures/share-fixture.json (built by
// build_share_fixture.py) and the fixture images. Every other request passes through.
//
//   GOOGLE_CLOUD_STORAGE_BUCKET=trend-trawler-deploy-ae \
//   NODE_OPTIONS="--import ./scripts/share-gcs-mock.mjs" \
//   HOSTNAME=127.0.0.1 PORT=3600 node .next/standalone/server.js

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const FIX = join(dirname(fileURLToPath(import.meta.url)), "screenshot-fixtures");
const { snapshot, images } = JSON.parse(readFileSync(join(FIX, "share-fixture.json"), "utf8"));
const realFetch = globalThis.fetch;

globalThis.fetch = async function mockedFetch(input, init) {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
  if (url.hostname === "metadata.google.internal") {
    return Response.json({ access_token: "screenshot-mock", expires_in: 3600, token_type: "Bearer" });
  }
  if (url.hostname === "storage.googleapis.com") {
    // /storage/v1/b/<bucket>/o/<url-encoded object>?alt=media
    const m = /^\/storage\/v1\/b\/[^/]+\/o\/([^/]+)$/.exec(url.pathname);
    const object = m ? decodeURIComponent(m[1]) : "";
    const parts = /^shares\/([^/]+)\/(.+)$/.exec(object);
    if (parts && parts[1] === snapshot.token) {
      if (parts[2] === "snapshot.json") {
        return new Response(JSON.stringify(snapshot), { headers: { "Content-Type": "application/json" } });
      }
      const img = /^(\d{1,2})\.jpg$/.exec(parts[2]);
      if (img && images[Number(img[1])]) {
        const body = readFileSync(join(FIX, "images", images[Number(img[1])]));
        return new Response(body, { headers: { "Content-Type": "image/jpeg" } });
      }
    }
    return new Response("Not found", { status: 404 });
  }
  return realFetch(input, init);
};

console.log(`[share-gcs-mock] serving share ${snapshot.token} (${images.length} creatives)`);
