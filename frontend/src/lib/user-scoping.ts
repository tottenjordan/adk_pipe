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

/** Returns the scoped upstream path/body, or null if the route isn't allowed. Throws on bad JSON.
 *  Segments arrive URL-decoded (Next's catch-all params), so each is re-encoded and dot
 *  segments are refused — otherwise a decoded `/` or `..` could walk out of the scoped path. */
export function scopeRequestToUser(
  method: string, path: string[], body: string | undefined, userId: string,
): { path: string; body: string | undefined } | null {
  if (path.some((s) => s === "." || s === "..")) return null;
  const route = ROUTES.find((r) => r.method === method && r.match(path));
  if (!route) return null;
  const segs = [...path];
  if (route.userAt !== undefined) segs[route.userAt] = userId;
  let outBody = body;
  if (route.bodyUser) outBody = JSON.stringify({ ...JSON.parse(body ?? "{}"), userId });
  return { path: segs.map(encodeURIComponent).join("/"), body: outBody };
}
