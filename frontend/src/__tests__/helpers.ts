// Shared test utilities. Not a test file: vitest's default include only picks
// up *.test.* / *.spec.*, so this module is imported, never collected.

/** A real JSON `Response` with the given body and status (default 200). */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
