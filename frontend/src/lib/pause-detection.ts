import type { AgentEvent } from "@/lib/types";

/** The paused long-running tool call a human review must answer. */
export interface PauseContext {
  functionCallId: string;
  functionName: string;
  eventId: string;
}

/**
 * Tracks long-running tool calls across a poll segment and reports the one
 * still UNANSWERED when the segment ends — that is the real pause.
 *
 * A long-running call alone is not a pause: since the P2 graph-Workflow
 * migration, the pipeline tools (e.g. `combined_research_pipeline`,
 * `understand_trends_agent_resilient`) are ADK `NodeTool`s, which are
 * themselves long-running, so their function-call events carry
 * `longRunningToolIds` too. They are answered by a `functionResponse` in the
 * same segment; a human-review checkpoint (`LongRunningFunctionTool`) is not.
 * The same rule makes a reload of a finished interactive run (replayed from
 * `since=0`) show "completed" instead of re-pausing at an answered checkpoint.
 */
export class PendingLongRunningCalls {
  private pending = new Map<string, PauseContext>();

  /** Record the event's long-running calls and clear any it answers. */
  observe(event: AgentEvent): void {
    // Skip partial (streaming) events — their function call IDs are
    // regenerated per chunk and won't match the session's final event.
    if (event.partial) return;
    const parts = event.content?.parts ?? [];
    const longRunning = event.longRunningToolIds ?? [];
    for (const part of parts) {
      const fc = part.functionCall;
      if (fc?.id && longRunning.includes(fc.id)) {
        this.pending.set(fc.id, {
          functionCallId: fc.id,
          functionName: fc.name ?? "",
          eventId: event.id,
        });
      }
      const fr = part.functionResponse;
      if (fr?.id) this.pending.delete(fr.id);
    }
  }

  /** The most recent still-unanswered long-running call, or null. */
  pause(): PauseContext | null {
    let last: PauseContext | null = null;
    for (const ctx of this.pending.values()) last = ctx;
    return last;
  }
}
