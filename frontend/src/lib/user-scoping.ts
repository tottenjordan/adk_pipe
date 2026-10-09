/** Routes the UI uses (src/lib/api.ts). `userAt` = index of the user segment to overwrite. */
const ROUTES: { method: string; match: (p: string[]) => boolean; userAt?: number; bodyUser?: true }[] = [
  { method: "GET", match: (p) => p.length === 1 && (p[0] === "list-apps" || p[0] === "health") },
  { method: "POST", match: (p) => p.length === 5 && p[0] === "apps" && p[2] === "users" && p[4] === "sessions", userAt: 3 },
  { method: "GET", match: (p) => p.length >= 5 && p[0] === "apps" && p[2] === "users" && p[4] === "sessions"
      && (p.length <= 6 || p[6] === "artifacts"), userAt: 3 },
  { method: "POST", match: (p) => p.length === 2 && p[0] === "runs", bodyUser: true },
  { method: "GET", match: (p) => p.length === 4 && p[0] === "runs", userAt: 2 },
  { method: "POST", match: (p) => p.length === 5 && p[0] === "runs" && p[4] === "resume", userAt: 2 },
  // Bandit experiments (docs/bandit/contracts.md §5, §8). Ids are validated below.
  { method: "POST", match: (p) => p.length === 1 && p[0] === "experiments", bodyUser: true },
  { method: "GET", match: (p) => p[0] === "experiments" && (p.length === 2 || p.length === 3
      || (p.length === 4 && (p[3] === "metrics" || p[3] === "creatives"))), userAt: 1 },
  { method: "POST", match: (p) => p.length === 4 && p[0] === "experiments"
      && (p[3] === "traffic" || p[3] === "stop"), userAt: 1 },
  // Human creative ratings (runserver/ratings.py): GET/PUT ratings/{u}/{session}, GET ratings/{u}/calibration.
  { method: "GET", match: (p) => p.length === 3 && p[0] === "ratings", userAt: 1 },
  { method: "PUT", match: (p) => p.length === 3 && p[0] === "ratings" && p[2] !== "calibration", userAt: 1 },
  // Share links (runserver/shares.py): POST shares/{u}/{app}/{session}, GET shares/{u},
  // DELETE shares/{u}/{token} — the only DELETE the proxy forwards. Ids validated below.
  { method: "POST", match: (p) => p.length === 4 && p[0] === "shares", userAt: 1 },
  { method: "GET", match: (p) => p.length === 2 && p[0] === "shares", userAt: 1 },
  { method: "DELETE", match: (p) => p.length === 3 && p[0] === "shares", userAt: 1 },
];

// ADK app names are Python identifiers (the agent package name).
const APP_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
/** Experiment ids: lowercase slug, 3–64 chars (the backend mints them). */
export const EXPERIMENT_ID_RE = /^[a-z0-9][a-z0-9-]{2,63}$/;
/** Session ids on the ratings routes: ADK mints UUIDs; allow a conservative slug. */
export const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
/** Share tokens (runserver/shares.py mints url-safe tokens). */
export const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{16,64}$/;
/** Query params the UI actually sends (poll `since`, artifact `version`); all else is dropped. */
const ALLOWED_QUERY = ["since", "version"] as const;

/** Returns the scoped upstream path/body, or null if the route isn't allowed. Throws on bad JSON.
 *  Segments arrive URL-decoded (Next's catch-all params), and the backend (uvicorn) would
 *  decode a re-encoded `%2F` again — so any segment holding `/` or `\` (or a dot segment) is
 *  refused outright: otherwise `runs/a%2Fbob%2F42%2Fresume` would match the kick-off shape
 *  here yet land on another user's resume route upstream. */
export function scopeRequestToUser(
  method: string, path: string[], body: string | undefined, userId: string,
): { path: string; body: string | undefined } | null {
  if (path.some((s) => s === "." || s === ".." || s.includes("/") || s.includes("\\"))) return null;
  if ((path[0] === "apps" || path[0] === "runs") && !APP_RE.test(path[1] ?? "")) return null;
  if (path[0] === "experiments" && path.length >= 3 && !EXPERIMENT_ID_RE.test(path[2])) return null;
  if (path[0] === "ratings" && path.length >= 3 && !SESSION_ID_RE.test(path[2])) return null;
  if (path[0] === "shares" && path.length === 4
      && (!APP_RE.test(path[2]) || !SESSION_ID_RE.test(path[3]))) return null;
  if (path[0] === "shares" && path.length === 3 && !SHARE_TOKEN_RE.test(path[2])) return null;
  const route = ROUTES.find((r) => r.method === method && r.match(path));
  if (!route) return null;
  const segs = [...path];
  if (route.userAt !== undefined) segs[route.userAt] = userId;
  let outBody = body;
  if (route.bodyUser) outBody = JSON.stringify({ ...JSON.parse(body ?? "{}"), userId });
  return { path: segs.map(encodeURIComponent).join("/"), body: outBody };
}

/** `run` (a traffic run number, contracts §10): an integer ≥ 1, only on the per-run experiment routes. */
const RUN_RE = /^[1-9][0-9]{0,5}$/;
const isRunRoute = (path: string[] | undefined) =>
  !!path && path.length === 4 && path[0] === "experiments" && (path[3] === "metrics" || path[3] === "creatives");

/** Rebuild a scoped request's query string from the allowlisted params only ("" if none).
 *  `run` is kept only for GET experiments/{u}/{id}/metrics|creatives and only as a plain
 *  positive integer; pass the request's path segments to allow it. */
export function scopeQuery(params: URLSearchParams, path?: string[]): string {
  const out = new URLSearchParams();
  for (const k of ALLOWED_QUERY) {
    const v = params.get(k);
    if (v !== null) out.set(k, v);
  }
  const run = params.get("run");
  if (run !== null && RUN_RE.test(run) && isRunRoute(path)) out.set("run", run);
  const q = out.toString();
  return q ? `?${q}` : "";
}
