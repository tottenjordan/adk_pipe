import type { Session, AgentEvent } from "./types";

// Route through the same-origin Next.js proxy (src/app/api/adk/[...path]/route.ts) so
// the browser never makes a cross-origin call — this avoids CORS and the Cloud
// Workstations port-auth redirect, while the proxy streams SSE responses through.
// Override with NEXT_PUBLIC_API_BASE to call an api_server directly if needed.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/adk";

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

export async function* streamRun(
  appName: string,
  userId: string,
  sessionId: string,
  message: string
): AsyncGenerator<AgentEvent> {
  const res = await fetch(`${API_BASE}/run_sse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      appName,
      userId,
      sessionId,
      newMessage: {
        role: "user",
        parts: [{ text: message }],
      },
      streaming: true,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to start run (${res.status}): ${body}`);
  }
  if (!res.body) throw new Error("No response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = line.slice(6).trim();
        if (data) {
          try {
            yield JSON.parse(data) as AgentEvent;
          } catch {
            // skip malformed JSON
          }
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Async-job run model
//
// The run is a detached background job: `startRun` kicks it off and returns
// immediately, then `pollRun` (or a one-shot `getRunStatus`) polls the job for
// only the events emitted since a cursor. This replaces the long-lived SSE
// generator (`streamRun`) so the run survives connection drops and Cloud Run
// CPU throttling between events.
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
  functionCallEventId?: string
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
      }),
    }
  );
  if (!res.ok) {
    throw new Error(`Failed to resume run (${res.status}): ${await res.text()}`);
  }
  return res.json();
}
