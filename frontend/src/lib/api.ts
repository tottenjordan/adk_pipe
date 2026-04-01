import type { Session, AgentEvent } from "./types";

// Call api_server directly (not through Next.js proxy) so SSE streams properly
const API_BASE = "http://localhost:8000";

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

export async function* resumeRun(
  appName: string,
  userId: string,
  sessionId: string,
  functionCallId: string,
  functionName: string,
  functionCallEventId: string,
  response: Record<string, unknown>
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
        parts: [
          {
            functionResponse: {
              id: functionCallId,
              name: functionName,
              response: response,
            },
          },
        ],
      },
      functionCallEventId,
      streaming: true,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to resume run (${res.status}): ${body}`);
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
