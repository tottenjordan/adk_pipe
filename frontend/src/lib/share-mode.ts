/**
 * SHARE_MODE route lockdown (pure; used by `src/proxy.ts`).
 *
 * The public `trend-trawler-share` service runs this same app with `SHARE_MODE=1`.
 * There, only the share pages, Next's static assets, `/robots.txt` and `/favicon.ico`
 * exist; everything else (every IAP page and, above all, every `/api/*` route —
 * `/api/gcs` would proxy the whole bucket) answers 404.
 *
 * Matching is done on the raw request pathname with anchored regexes and is never
 * decoded first, so `%2F`, `%2E%2E`, trailing slashes and similar shapes simply fail to
 * match (deny by default). WHATWG URL parsing has already collapsed literal `..`.
 */

/** Share token alphabet: what `secrets.token_urlsafe(16)` (22 chars) produces. */
export const TOKEN_PATTERN = "[A-Za-z0-9_-]{16,64}";

const SHARE_PATH_RE = new RegExp(`^/s/${TOKEN_PATTERN}(?:/img/[0-9]{1,2})?$`);

/** Exact public files (served from `src/app`). */
const PUBLIC_FILES = new Set(["/robots.txt", "/favicon.ico"]);

/** Next's hashed build assets; segments must stay inside the static tree. */
const STATIC_ASSET_RE = /^\/_next\/static\/[A-Za-z0-9_\-./~@]+$/;

export type RouteDecision = "share" | "allow" | "deny";

export function isShareModeEnabled(
  env: Record<string, string | undefined> = process.env
): boolean {
  return env.SHARE_MODE === "1";
}

/** `/s/<token>` or `/s/<token>/img/<n>` (n = 1–2 digits), nothing else. */
export function isSharePath(pathname: string): boolean {
  return SHARE_PATH_RE.test(pathname);
}

function isStaticAsset(pathname: string): boolean {
  if (!STATIC_ASSET_RE.test(pathname)) return false;
  // No dot segments, even though the URL parser normally removes them.
  return !pathname.split("/").some((seg) => seg === "." || seg === "..");
}

/** Dev server internals (HMR, error overlay); never reachable in a production build. */
function isDevInternal(pathname: string): boolean {
  if (pathname === "/_next/image" || pathname.startsWith("/_next/image/")) return false;
  return pathname.startsWith("/_next/") || pathname.startsWith("/__nextjs");
}

/**
 * - `share`: a share route; serve it with the share headers.
 * - `allow`: pass through untouched.
 * - `deny`: answer 404 (share mode only).
 *
 * Normal mode changes nothing except that share routes get the share headers
 * (the owner can preview a link behind IAP).
 */
export function decideRoute(
  pathname: string,
  opts: { shareMode: boolean; dev: boolean }
): RouteDecision {
  if (isSharePath(pathname)) return "share";
  if (!opts.shareMode) return "allow";
  if (PUBLIC_FILES.has(pathname) || isStaticAsset(pathname)) return "allow";
  if (opts.dev && isDevInternal(pathname)) return "allow";
  return "deny";
}

/** Response headers on every share route. */
export const SHARE_PAGE_HEADERS: Readonly<Record<string, string>> = {
  "X-Robots-Tag": "noindex, nofollow",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "private, max-age=60",
  "X-Content-Type-Options": "nosniff",
};

/**
 * Strict CSP for share pages. Scripts need a per-request nonce (Next's inline RSC
 * payload scripts carry it automatically when the request has this CSP header).
 * Styles keep `'unsafe-inline'`: React `style` attributes and the UI library's inline
 * CSS variables can't carry a nonce; no style-based exfiltration is possible with
 * `default-src 'self'` and no third-party origins.
 */
export function buildShareCsp(nonce: string, dev: boolean): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}
