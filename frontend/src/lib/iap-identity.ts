import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";

export const IAP_ISSUER = "https://cloud.google.com/iap";
const IAP_JWKS = createRemoteJWKSet(new URL("https://www.gstatic.com/iap/verify/public_key-jwk"));
// Same pattern as runserver.authz._EMAIL_RE (anchored = Python's fullmatch).
const EMAIL_RE = /^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$/;
const MD = "http://metadata.google.internal/computeMetadata/v1";

/** Must match runserver.authz.normalize_user_id exactly. */
export function normalizeUserId(raw: string): string {
  const v = raw.trim().toLowerCase().replace(/^accounts\.google\.com:/, "");
  if (!EMAIL_RE.test(v)) throw new Error(`not a valid user email: ${raw}`);
  return v;
}

/** Verify an `x-goog-iap-jwt-assertion` (ES256, IAP issuer, this service's audience) and
 *  return the normalized email. `allowedHd`, when given, pins the Workspace domain. */
export async function verifyIapJwt(
  token: string, opts: { audience: string; keys?: JWTVerifyGetKey; allowedHd?: string },
): Promise<string> {
  const { payload } = await jwtVerify(token, opts.keys ?? IAP_JWKS, {
    issuer: IAP_ISSUER, audience: opts.audience, algorithms: ["ES256"], clockTolerance: 30,
    requiredClaims: ["exp", "iat", "email"],
  });
  if (payload.email_verified === false) throw new Error("IAP JWT email is not verified");
  if (opts.allowedHd !== undefined && payload.hd !== opts.allowedHd) {
    throw new Error(`IAP JWT hd ${JSON.stringify(payload.hd)} is not allowed`);
  }
  if (typeof payload.email !== "string") throw new Error("IAP JWT has no email");
  return normalizeUserId(payload.email);
}

let audiencePromise: Promise<string> | undefined;
/** Test hook: drop the cached audience. */
export function resetIapAudienceCache(): void { audiencePromise = undefined; }

async function metadata(p: string): Promise<string> {
  const res = await fetch(`${MD}/${p}`, {
    headers: { "Metadata-Flavor": "Google" }, signal: AbortSignal.timeout(1000),
  });
  if (!res.ok) throw new Error(`metadata ${p}: HTTP ${res.status}`);
  return (await res.text()).trim();
}

/** `/projects/N/locations/REGION/services/K_SERVICE` from the metadata server
 *  (no env vars → web redeploys stay env-flag-free); IAP_AUDIENCE overrides. Only a fully
 *  resolved value is cached — any failure clears the cache so the next call retries. */
export function iapAudience(): Promise<string> {
  if (process.env.IAP_AUDIENCE) return Promise.resolve(process.env.IAP_AUDIENCE);
  const p = (audiencePromise ??= (async () => {
    const service = process.env.K_SERVICE;
    if (!service) throw new Error("K_SERVICE unset");
    const num = await metadata("project/numeric-project-id");
    const region = (await metadata("instance/region")).split("/").pop(); // projects/N/regions/R
    if (!num || !region) throw new Error("metadata returned an empty project number or region");
    return `/projects/${num}/locations/${region}/services/${service}`;
  })());
  p.catch(() => { if (audiencePromise === p) audiencePromise = undefined; });
  return p;
}

export type ResolvedUser =
  | { kind: "user"; userId: string } | { kind: "local" } | { kind: "reject"; status: 401 };

/** Turn the inbound request's IAP assertion into a user id. On Cloud Run a missing/invalid
 *  assertion — or an unconfigured `allowedHd` — fails closed; locally (no assertion) the
 *  request passes through unscoped. The unsigned `x-goog-authenticated-user-*` headers are
 *  spoofable and never consulted. */
export async function resolveUser(
  headers: Headers,
  opts: {
    onCloudRun: boolean;
    allowedHd?: string;
    verify?: (token: string, allowedHd?: string) => Promise<string>;
  },
): Promise<ResolvedUser> {
  const token = headers.get("x-goog-iap-jwt-assertion");
  if (!token) return opts.onCloudRun ? { kind: "reject", status: 401 } : { kind: "local" };
  if (opts.onCloudRun && !opts.allowedHd) {
    console.error("IAP_ALLOWED_HD unset");
    return { kind: "reject", status: 401 };
  }
  const verify = opts.verify ?? (async (t: string, hd?: string) =>
    verifyIapJwt(t, { audience: await iapAudience(), allowedHd: hd }));
  try {
    return { kind: "user", userId: await verify(token, opts.allowedHd || undefined) };
  } catch (err) {
    console.warn("IAP JWT rejected:", (err as Error).message);
    return { kind: "reject", status: 401 };
  }
}
