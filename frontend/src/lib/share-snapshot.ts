/**
 * Public share snapshots (server only).
 *
 * The api freezes a share into `gs://<bucket>/shares/<token>/snapshot.json` plus
 * `<i>.png` (contract v1, docs/plans/2026-10-09-shareable-links.md). The public share
 * service reads exactly those objects: every GCS URL is built here from a validated
 * token and a fixed file name, never from request input. Its service account can only
 * read `shares/` anyway (IAM condition), so this is defence in depth.
 */

import { getAccessToken } from "@/lib/gcp-auth";

export const TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;

export const MAX_CREATIVES = 4;
const SHORT_MAX = 300;
const LONG_MAX = 4000;
const MAX_CHECKS = 24;
const MAX_SNAPSHOT_BYTES = 512 * 1024;
const IMAGE_INDEX_RE = /^(?:0|[1-9][0-9]?)$/;
const ASPECT_RE = /^[1-9][0-9]?:[1-9][0-9]?$/;
const BUCKET_RE = /^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$/;

export interface ShareCheck {
  /** Which half of the creative the check is about (the backend sends both halves). */
  kind?: "copy" | "visual";
  gate: string;
  label: string;
  passed: boolean;
  advisory: boolean;
}

export interface ShareVerdict {
  passed: boolean | null;
  score: number | null;
}

export interface ShareEval {
  /** null = the judge returned no verdict for this creative. */
  passed: boolean | null;
  score: number | null;
  checks: ShareCheck[];
  copy?: ShareVerdict | null;
  visual?: ShareVerdict | null;
}

export interface ShareCreative {
  index: number;
  image: string;
  aspect_ratio: string;
  alt: string;
  visual_style: string;
  headline: string;
  body: string;
  caption: string;
  cta: string;
  tone: string;
  eval?: ShareEval;
}

export interface ShareSnapshotV1 {
  version: 1;
  token: string;
  created_at: string;
  scope: "slate" | "creative";
  brand: string;
  product: string;
  trend: string;
  include_eval: boolean;
  creatives: ShareCreative[];
}

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const isText = (v: unknown, max: number): v is string =>
  typeof v === "string" && v.length <= max;

const isScore = (v: unknown): boolean =>
  v === null || (typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1);

function isVerdict(v: unknown): boolean {
  if (v === undefined || v === null) return true;
  return isObj(v) && (v.passed === null || typeof v.passed === "boolean") && isScore(v.score);
}

function isCheck(v: unknown): v is ShareCheck {
  return (
    isObj(v) &&
    (v.kind === undefined || v.kind === "copy" || v.kind === "visual") &&
    isText(v.gate, SHORT_MAX) &&
    isText(v.label, SHORT_MAX) &&
    typeof v.passed === "boolean" &&
    typeof v.advisory === "boolean"
  );
}

function isEval(v: unknown): v is ShareEval {
  if (!isObj(v) || !(v.passed === null || typeof v.passed === "boolean")) return false;
  if (!isScore(v.score) || !isVerdict(v.copy) || !isVerdict(v.visual)) return false;
  return Array.isArray(v.checks) && v.checks.length <= MAX_CHECKS && v.checks.every(isCheck);
}

function isCreative(v: unknown, position: number, includeEval: boolean): v is ShareCreative {
  if (!isObj(v)) return false;
  if (v.index !== position) return false; // integer, in order: the image route trusts it
  if (!isText(v.image, SHORT_MAX)) return false;
  if (!isText(v.aspect_ratio, 8) || (v.aspect_ratio !== "" && !ASPECT_RE.test(v.aspect_ratio))) {
    return false;
  }
  for (const key of ["visual_style", "headline", "cta", "tone"]) {
    if (!isText(v[key], SHORT_MAX)) return false;
  }
  for (const key of ["alt", "body", "caption"]) {
    if (!isText(v[key], LONG_MAX)) return false;
  }
  if (v.eval !== undefined && v.eval !== null) {
    if (!includeEval || !isEval(v.eval)) return false;
  }
  return true;
}

/** Defensive v1 shape check: allowlisted fields, capped strings, ≤ 4 ordered creatives. */
export function isSnapshotV1(v: unknown): v is ShareSnapshotV1 {
  if (!isObj(v) || v.version !== 1) return false;
  if (v.scope !== "slate" && v.scope !== "creative") return false;
  if (typeof v.include_eval !== "boolean") return false;
  for (const key of ["token", "created_at", "brand", "product", "trend"]) {
    if (!isText(v[key], SHORT_MAX)) return false;
  }
  const creatives = v.creatives;
  if (!Array.isArray(creatives) || creatives.length < 1 || creatives.length > MAX_CREATIVES) {
    return false;
  }
  const includeEval = v.include_eval;
  return creatives.every((c, i) => isCreative(c, i, includeEval));
}

/** The share bucket: `GOOGLE_CLOUD_STORAGE_BUCKET` (the name the api ships; `gs://` tolerated). */
export function shareBucket(env: Record<string, string | undefined> = process.env): string {
  const raw = (env.GOOGLE_CLOUD_STORAGE_BUCKET ?? "").trim().replace(/^gs:\/\//, "");
  if (!BUCKET_RE.test(raw)) throw new Error("GOOGLE_CLOUD_STORAGE_BUCKET is unset or invalid");
  return raw;
}

/** JSON-API media URL for `shares/<token>/<file>` (file = snapshot.json or `<n>.png`). */
export function shareObjectUrl(
  bucket: string,
  token: string,
  file: "snapshot.json" | number
): string {
  if (!TOKEN_RE.test(token)) throw new Error("invalid share token");
  if (typeof file === "number" && !(Number.isInteger(file) && file >= 0 && file < 100)) {
    throw new Error("invalid image index");
  }
  const name = typeof file === "number" ? `${file}.png` : file;
  const object = encodeURIComponent(`shares/${token}/${name}`);
  return `https://storage.googleapis.com/storage/v1/b/${bucket}/o/${object}?alt=media`;
}

export interface GcsDeps {
  fetch?: typeof fetch;
  getToken?: () => Promise<string>;
  bucket?: string;
}

async function gcsGet(token: string, file: "snapshot.json" | number, deps: GcsDeps) {
  const bucket = deps.bucket ?? shareBucket();
  const url = shareObjectUrl(bucket, token, file);
  const accessToken = await (deps.getToken ?? getAccessToken)();
  return (deps.fetch ?? fetch)(url, {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
  });
}

/**
 * The validated snapshot, or null when the token is malformed, the snapshot is gone
 * (revoked links delete their objects) or it fails validation. Other storage errors
 * throw, so an outage isn't shown as "no longer available".
 */
export async function loadSnapshot(
  token: string,
  deps: GcsDeps = {}
): Promise<ShareSnapshotV1 | null> {
  if (!TOKEN_RE.test(token)) return null;
  const res = await gcsGet(token, "snapshot.json", deps);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`share snapshot fetch failed: ${res.status}`);
  const text = await res.text();
  if (text.length > MAX_SNAPSHOT_BYTES) return null;
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isSnapshotV1(data) || data.token !== token) return null;
  return data;
}

const notFound = () =>
  new Response("Not found", {
    status: 404,
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
  });

/**
 * The share image response for `/s/<token>/img/<n>`: 404 unless the token is valid, n is
 * a plain index below the snapshot's creative count and the object exists.
 */
export async function loadShareImage(
  token: string,
  n: string,
  deps: GcsDeps = {}
): Promise<Response> {
  if (!TOKEN_RE.test(token) || !IMAGE_INDEX_RE.test(n)) return notFound();
  const snapshot = await loadSnapshot(token, deps);
  const index = Number(n);
  if (!snapshot || index >= snapshot.creatives.length) return notFound();
  const res = await gcsGet(token, index, deps);
  if (res.status === 404) return notFound();
  if (!res.ok || !res.body) {
    return new Response("Unavailable", { status: 502, headers: { "Cache-Control": "no-store" } });
  }
  return new Response(res.body, {
    status: 200,
    headers: {
      "Content-Type": "image/png",
      "Cache-Control": "private, max-age=60",
      "X-Content-Type-Options": "nosniff",
    },
  });
}
