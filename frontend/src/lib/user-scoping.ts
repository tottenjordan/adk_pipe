/** Routes the UI uses (src/lib/api.ts). `userAt` = index of the user segment to overwrite. */
const ROUTES: { method: string; match: (p: string[]) => boolean; userAt?: number; bodyUser?: true }[] = [
  { method: "GET", match: (p) => p.length === 1 && (p[0] === "list-apps" || p[0] === "health") },
  { method: "POST", match: (p) => p.length === 5 && p[0] === "apps" && p[2] === "users" && p[4] === "sessions", userAt: 3 },
  { method: "GET", match: (p) => p.length >= 5 && p[0] === "apps" && p[2] === "users" && p[4] === "sessions"
      && (p.length <= 6 || p[6] === "artifacts"), userAt: 3 },
  { method: "POST", match: (p) => p.length === 2 && p[0] === "runs", bodyUser: true },
  { method: "GET", match: (p) => p.length === 4 && p[0] === "runs", userAt: 2 },
  { method: "POST", match: (p) => p.length === 5 && p[0] === "runs" && p[4] === "resume", userAt: 2 },
];

// ADK app names are Python identifiers (the agent package name).
const APP_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
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
  const route = ROUTES.find((r) => r.method === method && r.match(path));
  if (!route) return null;
  const segs = [...path];
  if (route.userAt !== undefined) segs[route.userAt] = userId;
  let outBody = body;
  if (route.bodyUser) outBody = JSON.stringify({ ...JSON.parse(body ?? "{}"), userId });
  return { path: segs.map(encodeURIComponent).join("/"), body: outBody };
}

/** Rebuild a scoped request's query string from the allowlisted params only ("" if none). */
export function scopeQuery(params: URLSearchParams): string {
  const out = new URLSearchParams();
  for (const k of ALLOWED_QUERY) {
    const v = params.get(k);
    if (v !== null) out.set(k, v);
  }
  const q = out.toString();
  return q ? `?${q}` : "";
}
