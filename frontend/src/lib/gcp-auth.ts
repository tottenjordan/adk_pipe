import "server-only";
import { execFile } from "node:child_process";

const METADATA_BASE =
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default";
const METADATA_HEADERS = { "Metadata-Flavor": "Google" };
const METADATA_TIMEOUT_MS = 2000;
const GCLOUD_TIMEOUT_MS = 15_000;
/** Refresh a cached token this long before it expires. */
const EXPIRY_MARGIN_MS = 60_000;
/** gcloud's access token carries no expiry; real ones last an hour, so re-ask often. */
const GCLOUD_ACCESS_TOKEN_TTL_MS = 5 * 60_000;

type Cached = { token: string; expiresAt: number };

// Per-process, in-memory caches: every poll and every proxied image used to cost a
// metadata round trip.
let accessCache: Cached | null = null;
const identityCache = new Map<string, Cached>();

/** Test-only: forget every cached token. */
export function resetTokenCacheForTests(): void {
  accessCache = null;
  identityCache.clear();
}

function fresh(entry: Cached | null | undefined): string | null {
  return entry && Date.now() < entry.expiresAt ? entry.token : null;
}

/** Cache entry ending `EXPIRY_MARGIN_MS` before `expiresAtMs`, or null if that is past. */
function entry(token: string, expiresAtMs: number | null): Cached | null {
  if (expiresAtMs === null) return null;
  const expiresAt = expiresAtMs - EXPIRY_MARGIN_MS;
  return expiresAt > Date.now() ? { token, expiresAt } : null;
}

/** `exp` (ms) from a JWT's base64url payload; null when unreadable. */
function jwtExpiryMs(token: string): number | null {
  try {
    const payload = JSON.parse(Buffer.from(token.split(".")[1] ?? "", "base64url").toString());
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

/** The gcloud CLI, asynchronously (never blocks the event loop). Local dev only: on
 *  Cloud Run there is no gcloud in the image, so a metadata failure is an error. */
async function gcloud(args: string[], metadataError: unknown): Promise<string> {
  if (process.env.K_SERVICE) {
    const detail = metadataError instanceof Error ? metadataError.message : String(metadataError);
    throw new Error(`GCP credentials unavailable: the metadata server failed (${detail})`);
  }
  return new Promise((resolve, reject) => {
    execFile("gcloud", args, { encoding: "utf-8", timeout: GCLOUD_TIMEOUT_MS }, (err, stdout) => {
      if (err) reject(err);
      else resolve(String(stdout).trim());
    });
  });
}

/** OAuth access token for calling Google APIs (e.g. Cloud Storage). Metadata server on
 *  Cloud Run; falls back to the gcloud CLI for local dev. Cached until 60 s before expiry. */
export async function getAccessToken(): Promise<string> {
  const cached = fresh(accessCache);
  if (cached) return cached;
  try {
    const res = await fetch(`${METADATA_BASE}/token`, {
      headers: METADATA_HEADERS,
      signal: AbortSignal.timeout(METADATA_TIMEOUT_MS),
    });
    if (!res.ok) throw new Error(`metadata token HTTP ${res.status}`);
    const body = (await res.json()) as { access_token: string; expires_in?: number };
    const expiresAtMs =
      typeof body.expires_in === "number" ? Date.now() + body.expires_in * 1000 : null;
    accessCache = entry(body.access_token, expiresAtMs);
    return body.access_token;
  } catch (err) {
    const token = await gcloud(["auth", "application-default", "print-access-token"], err);
    accessCache = { token, expiresAt: Date.now() + GCLOUD_ACCESS_TOKEN_TTL_MS };
    return token;
  }
}

/** Google-signed ID token whose `aud` is `audience` — used to call a private Cloud Run
 *  service. The metadata `identity` endpoint returns the raw JWT as text. `format=full`
 *  makes it carry `email`/`email_verified`, which the backend needs to recognise the proxy
 *  SA before trusting `X-TT-User`. Cached per audience until 60 s before the JWT's `exp`. */
export async function getIdentityToken(audience: string): Promise<string> {
  const cached = fresh(identityCache.get(audience));
  if (cached) return cached;
  let token: string;
  try {
    const res = await fetch(
      `${METADATA_BASE}/identity?audience=${encodeURIComponent(audience)}&format=full`,
      { headers: METADATA_HEADERS, signal: AbortSignal.timeout(METADATA_TIMEOUT_MS) },
    );
    if (!res.ok) throw new Error(`metadata identity HTTP ${res.status}`);
    token = (await res.text()).trim();
  } catch (err) {
    token = await gcloud(["auth", "print-identity-token"], err);
  }
  const cache = entry(token, jwtExpiryMs(token));
  if (cache) identityCache.set(audience, cache);
  else identityCache.delete(audience);
  return token;
}
