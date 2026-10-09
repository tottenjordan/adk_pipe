import type { Session, AgentEvent } from "./types";
import type { Calibration, Rating, RatingPayload } from "./ratings";
import { ShareError, type CreateSharePayload, type Share } from "./shares";
import {
  PersonRefError,
  type CreatePersonRefPayload,
  type PersonRef,
  type PersonRefList,
} from "./person-refs";

// Route through the same-origin Next.js proxy (src/app/api/adk/[...path]/route.ts) so
// the browser never makes a cross-origin call — this avoids CORS and the Cloud
// Workstations port-auth redirect, while the proxy streams SSE responses through.
// Override with NEXT_PUBLIC_API_BASE to call an api_server directly if needed.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/adk";

/** Placeholder `userId` sent by every client call; the /api/adk proxy substitutes the
 *  IAP-verified user (locally, with no IAP, it passes through as-is). */
export const SELF_USER_ID = "me";

/**
 * Extract a human-readable error from a streamed run event, or null if the
 * event is not an error. The ADK run_sse stream reports model/agent failures
 * as data events (errorCode/errorMessage, or a terminal bare `error`) that
 * carry no `content`, so a content-only consumer would drop them silently and
 * the run would appear to stall. Callers should surface the returned message.
 */
export function getEventError(event: AgentEvent): string | null {
  const raw = event.errorMessage || event.errorCode || event.error;
  if (!raw) return null;
  // A model 429 is the common case on the shared per-minute Vertex quota —
  // give an actionable message instead of a raw stack-trace fragment.
  if (/429|RESOURCE_EXHAUSTED|ResourceExhausted/.test(raw)) {
    return "Vertex AI quota exhausted (429): the shared per-minute request quota was hit. Wait a minute and retry, and avoid running multiple agents at once.";
  }
  return raw;
}

export async function listApps(): Promise<string[]> {
  const res = await fetch(`${API_BASE}/list-apps`);
  if (!res.ok) throw new Error(`Failed to list apps: ${res.statusText}`);
  return res.json();
}

export async function createSession(
  appName: string,
  userId: string,
  state?: Record<string, unknown>
): Promise<Session> {
  const res = await fetch(
    `${API_BASE}/apps/${appName}/users/${userId}/sessions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
    }
  );
  if (!res.ok) throw new Error(`Failed to create session: ${res.statusText}`);
  return res.json();
}

export async function getSession(
  appName: string,
  userId: string,
  sessionId: string
): Promise<Session> {
  const res = await fetch(
    `${API_BASE}/apps/${appName}/users/${userId}/sessions/${sessionId}`
  );
  if (!res.ok) throw new Error(`Failed to get session: ${res.statusText}`);
  return res.json();
}

export async function listSessions(
  appName: string,
  userId: string
): Promise<Session[]> {
  const res = await fetch(
    `${API_BASE}/apps/${appName}/users/${userId}/sessions`
  );
  if (!res.ok) throw new Error(`Failed to list sessions: ${res.statusText}`);
  return res.json();
}

export async function listArtifacts(
  appName: string,
  userId: string,
  sessionId: string
): Promise<string[]> {
  const res = await fetch(
    `${API_BASE}/apps/${appName}/users/${userId}/sessions/${sessionId}/artifacts`
  );
  if (!res.ok) throw new Error(`Failed to list artifacts: ${res.statusText}`);
  return res.json();
}

export async function getArtifact(
  appName: string,
  userId: string,
  sessionId: string,
  artifactName: string
): Promise<unknown> {
  const res = await fetch(
    `${API_BASE}/apps/${appName}/users/${userId}/sessions/${sessionId}/artifacts/${artifactName}`
  );
  if (!res.ok) throw new Error(`Failed to get artifact: ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Async-job run model
//
// The run is a detached background job: `startRun` kicks it off and returns
// immediately, then `pollRun` (or a one-shot `getRunStatus`) polls the job for
// only the events emitted since a cursor. This replaces a long-lived SSE
// generator so the run survives connection drops and Cloud Run CPU throttling
// between events.
// ---------------------------------------------------------------------------

/** One poll payload from `GET /runs/{app}/{user}/{sid}?since=N`. */
export interface PollResult {
  /** "running" | "done" | "error" | "not_found". */
  status: string;
  /** Only the NEW events since the requested cursor. */
  events: AgentEvent[];
  /** New absolute cursor (total event count) to pass as the next `since`. */
  nextCursor: number;
  /** Merged session state dict (present so callers can seed the sidebar). */
  state?: Record<string, unknown>;
  /** Error string on `status === "error"`, else null. */
  error?: string | null;
}

/**
 * Start a detached background run. Returns as soon as the job is registered;
 * consume its output with `pollRun` (seed state with `getRunStatus` first).
 */
export async function startRun(
  appName: string,
  userId: string,
  sessionId: string,
  message: string
): Promise<{ runId: string; status: string }> {
  const res = await fetch(`${API_BASE}/runs/${appName}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userId, sessionId, message }),
  });
  // 409 = the server's duplicate-run guard: a run is ALREADY active for this
  // session (e.g. an earlier POST landed but its response was lost). The run is
  // live, so treat it as started and let the caller poll it.
  if (res.status === 409) return { runId: sessionId, status: "running" };
  if (!res.ok) {
    throw new Error(`Failed to start run (${res.status}): ${await res.text()}`);
  }
  return res.json();
}

/**
 * One-shot poll — fetch the run status, new events since `since`, and the merged
 * session state in a single GET. Task 8 calls this once (since=0) to seed the
 * sidebar before entering the `pollRun` loop.
 */
export async function getRunStatus(
  appName: string,
  userId: string,
  sessionId: string,
  since = 0
): Promise<PollResult> {
  const res = await fetch(
    `${API_BASE}/runs/${appName}/${userId}/${sessionId}?since=${since}`
  );
  if (!res.ok) throw new Error(`Failed to poll run (${res.status})`);
  return res.json();
}

/**
 * Poll a detached run to completion, yielding each new event exactly once.
 * Loops on the `since` cursor until a terminal status ("done"), throwing on
 * "error". "not_found" is transient (the run may not be registered yet), so we
 * keep waiting — same as "running". Pass `opts.signal` to cancel on unmount.
 */
export async function* pollRun(
  appName: string,
  userId: string,
  sessionId: string,
  opts: { intervalMs?: number; signal?: AbortSignal } = {}
): AsyncGenerator<AgentEvent> {
  const intervalMs = opts.intervalMs ?? 1500;
  let since = 0;
  for (;;) {
    const res = await fetch(
      `${API_BASE}/runs/${appName}/${userId}/${sessionId}?since=${since}`,
      { signal: opts.signal }
    );
    if (!res.ok) throw new Error(`Failed to poll run (${res.status})`);
    const data: PollResult = await res.json();
    for (const ev of data.events ?? []) yield ev;
    since = data.nextCursor ?? since + (data.events?.length ?? 0);
    if (data.status === "error") throw new Error(data.error || "run failed");
    // "not_found" is transient (run not yet registered) — keep waiting.
    if (data.status !== "running" && data.status !== "not_found") return;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/**
 * Thrown by `resumeRun` when the server's duplicate-run guard rejected the resume
 * WITHOUT applying it (409 `prior_segment_active`: the previous segment was still
 * finishing after the server's grace wait). The run is still paused at the same
 * checkpoint, so the caller should re-offer the review for the user to re-submit
 * — polling would neither re-show the (already-deduped) review panel nor apply
 * the response.
 */
export class ResumeNotAppliedError extends Error {
  constructor(
    message = "The previous step is still finishing, so your review wasn't applied yet. Please wait a moment and submit it again."
  ) {
    super(message);
    this.name = "ResumeNotAppliedError";
  }
}

/**
 * Thrown by `resumeRun` when the server rejected the review's edits as invalid
 * (400, e.g. `invalid_brief` at checkpoint 1) WITHOUT applying anything. Like
 * `ResumeNotAppliedError` the run is still paused, so the caller re-offers the
 * review; the message names the offending fields.
 */
export class ResumeRejectedError extends ResumeNotAppliedError {
  /** Per-field errors keyed by the edited object's path (e.g. `angles.0.name`). */
  readonly fieldErrors: Record<string, string>;

  constructor(message: string, fieldErrors: Record<string, string> = {}) {
    super(message);
    this.name = "ResumeRejectedError";
    this.fieldErrors = fieldErrors;
  }
}

/** A 400 body as a `ResumeRejectedError` (message + `detail.errors` by field). */
async function rejection(res: Response): Promise<ResumeRejectedError> {
  const fallback = "The review's edits were invalid and weren't applied. Fix them and submit again.";
  try {
    const detail = (await res.json())?.detail;
    const fieldErrors: Record<string, string> = {};
    if (Array.isArray(detail?.errors)) {
      for (const e of detail.errors) {
        if (typeof e?.loc === "string" && typeof e?.msg === "string") fieldErrors[e.loc] ??= e.msg;
      }
    }
    const message = typeof detail?.message === "string" && detail.message ? `${detail.message} Fix it and submit again.` : fallback;
    return new ResumeRejectedError(message, fieldErrors);
  } catch {
    return new ResumeRejectedError(fallback);
  }
}

/** Machine-readable `detail.reason` from a 409 body, or null if absent. */
async function conflictReason(res: Response): Promise<string | null> {
  try {
    const body = await res.json();
    const reason = body?.detail?.reason;
    return typeof reason === "string" ? reason : null;
  } catch {
    return null;
  }
}

/**
 * Resume a paused interactive run by submitting a human-review function
 * response. The server builds the `functionResponse` message and re-launches
 * the detached job; the caller re-enters `pollRun` to consume new events.
 */
export async function resumeRun(
  appName: string,
  userId: string,
  sessionId: string,
  functionCallId: string,
  functionName: string,
  response: Record<string, unknown>,
  functionCallEventId?: string,
  edits?: Record<string, unknown>[]
): Promise<{ runId: string; status: string }> {
  const res = await fetch(
    `${API_BASE}/runs/${appName}/${userId}/${sessionId}/resume`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        functionCallId,
        functionName,
        response,
        functionCallEventId,
        // Checkpoint edits (1: report / structured brief; 3: concepts): merged
        // into session state server-side before the resumed run (see
        // runserver.async_runs). Omitted when absent/empty.
        ...(edits && edits.length ? { edits } : {}),
      }),
    }
  );
  // 409 = the server's duplicate-run guard. `resume_in_progress`: a resume for
  // this same checkpoint (double-submit / retried POST) is already running, so
  // the user's intent is being served — treat as running and let the caller
  // poll (like startRun). Anything else (`prior_segment_active`, or an
  // unrecognised body) means this response was NOT applied: surface it so the
  // caller can re-offer the review (re-submitting is safe either way).
  if (res.status === 409) {
    if ((await conflictReason(res)) === "resume_in_progress") {
      return { runId: sessionId, status: "running" };
    }
    throw new ResumeNotAppliedError();
  }
  if (res.status === 400) throw await rejection(res);
  if (!res.ok) {
    throw new Error(`Failed to resume run (${res.status}): ${await res.text()}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Human creative ratings (runserver/ratings.py; judge calibration)
// ---------------------------------------------------------------------------

const ratingsUrl = (tail: string) =>
  `${API_BASE}/ratings/${SELF_USER_ID}/${encodeURIComponent(tail)}`;

/** `PUT /ratings/{user}/{session}`: upsert the caller's rating of one creative. */
export async function putRating(
  sessionId: string,
  payload: RatingPayload
): Promise<Rating> {
  const res = await fetch(ratingsUrl(sessionId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to save rating (${res.status})`);
  return res.json();
}

/** `GET /ratings/{user}/{session}`: the caller's ratings for one run. */
export async function getRatings(sessionId: string): Promise<Rating[]> {
  const res = await fetch(ratingsUrl(sessionId));
  if (!res.ok) throw new Error(`Failed to load ratings (${res.status})`);
  const data = await res.json();
  return Array.isArray(data?.ratings) ? data.ratings : [];
}

/** `GET /ratings/{user}/calibration`: judge-human agreement over the caller's ratings. */
export async function getCalibration(): Promise<Calibration> {
  const res = await fetch(ratingsUrl("calibration"));
  if (!res.ok) throw new Error(`Failed to load calibration (${res.status})`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Share links (runserver/shares.py): frozen, revocable public snapshots
// ---------------------------------------------------------------------------

const sharesUrl = (...tail: string[]) =>
  [`${API_BASE}/shares/${SELF_USER_ID}`, ...tail.map(encodeURIComponent)].join("/");

/** A failed shares response as a `ShareError` (reason from `detail.reason`, if any). */
async function shareError(res: Response): Promise<ShareError> {
  let reason: string | null = null;
  try {
    const r = (await res.json())?.detail?.reason;
    if (typeof r === "string" && r) reason = r;
  } catch {
    // non-JSON body (e.g. the proxy's plain 404)
  }
  return new ShareError(reason, res.status);
}

/** `POST /shares/{user}/{app}/{session}`: freeze the slate (or `concept_names`) into a link. */
export async function createShare(
  appName: string,
  sessionId: string,
  payload: CreateSharePayload
): Promise<Share> {
  const res = await fetch(sharesUrl(appName, sessionId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw await shareError(res);
  return res.json();
}

/** `GET /shares/{user}`: the caller's active share links, newest first. */
export async function listShares(): Promise<Share[]> {
  const res = await fetch(sharesUrl());
  if (!res.ok) throw await shareError(res);
  const data = await res.json();
  return Array.isArray(data?.shares) ? data.shares : [];
}

/** `DELETE /shares/{user}/{token}`: revoke a link (idempotent server-side). */
export async function revokeShare(token: string): Promise<void> {
  const res = await fetch(sharesUrl(token), { method: "DELETE" });
  if (!res.ok) throw await shareError(res);
}

// ---------------------------------------------------------------------------
// Person references (runserver/person_refs.py): consented photos of people
// ---------------------------------------------------------------------------

const personRefsUrl = (...tail: string[]) =>
  [`${API_BASE}/person-refs/${SELF_USER_ID}`, ...tail.map(encodeURIComponent)].join("/");

/** A failed person-refs response as a `PersonRefError` (reason from `detail.reason`). */
async function personRefError(res: Response): Promise<PersonRefError> {
  let reason: string | null = null;
  try {
    const r = (await res.json())?.detail?.reason;
    if (typeof r === "string" && r) reason = r;
  } catch {
    // non-JSON body (e.g. the proxy's plain 404)
  }
  return new PersonRefError(reason, res.status);
}

/** `GET /person-refs/{user}`: the caller's active consents (newest first) and upload prefix. */
export async function listPersonRefs(): Promise<PersonRefList> {
  const res = await fetch(personRefsUrl());
  if (!res.ok) throw await personRefError(res);
  const data = await res.json();
  return {
    personRefs: Array.isArray(data?.person_refs) ? data.person_refs : [],
    prefix: typeof data?.prefix === "string" && data.prefix ? data.prefix : null,
  };
}

/** `POST /person-refs/{user}`: register a consented photo. */
export async function createPersonRef(payload: CreatePersonRefPayload): Promise<PersonRef> {
  const res = await fetch(personRefsUrl(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw await personRefError(res);
  return res.json();
}

/** `DELETE /person-refs/{user}/{consent_id}`: revoke, deleting the photo (idempotent). */
export async function revokePersonRef(consentId: string): Promise<void> {
  const res = await fetch(personRefsUrl(consentId), { method: "DELETE" });
  if (!res.ok) throw await personRefError(res);
}
