/**
 * The one place an expired IAP sign-in is recognised. Behind IAP an expired
 * session never reaches the app: IAP answers the same-origin `/api/adk` call
 * with a 302 to Google sign-in, which a following fetch turns into an opaque
 * CORS failure (indistinguishable from a network error). Fetching with
 * `redirect: "manual"` keeps that redirect visible as an `opaqueredirect`
 * response; the proxy itself answers 401 when the IAP assertion is missing or
 * invalid. Both mean "sign in again", and a reload does that. A 403 is a
 * signed-in caller who isn't allowed, so it stays an ordinary error.
 */

export const SESSION_EXPIRED_MESSAGE = "Your sign-in expired. Reload to continue.";

export class SessionExpiredError extends Error {
  constructor() {
    super(SESSION_EXPIRED_MESSAGE);
    this.name = "SessionExpiredError";
  }
}

export function isSessionExpired(err: unknown): err is SessionExpiredError {
  return err instanceof SessionExpiredError;
}

/** `fetch` for the API: throws `SessionExpiredError` on a 401 or an IAP sign-in redirect. */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(input, { ...init, redirect: "manual" });
  if (res.status === 401 || res.type === "opaqueredirect") throw new SessionExpiredError();
  return res;
}
