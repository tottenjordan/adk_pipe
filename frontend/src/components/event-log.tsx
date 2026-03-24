"use client";

import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { AgentEvent } from "@/lib/types";
import { useEffect, useRef } from "react";

function summarizeArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  return entries
    .map(([k, v]) => {
      const val = typeof v === "string" ? v : JSON.stringify(v);
      const truncated = val.length > 80 ? val.slice(0, 80) + "..." : val;
      return `${k}: ${truncated}`;
    })
    .join(", ");
}

function EventItem({ event }: { event: AgentEvent }) {
  const parts = event.content?.parts || [];

  return (
    <div className="border-b border-border px-4 py-3 last:border-0">
      <div className="mb-1 flex items-center gap-2">
        <Badge variant="outline" className="text-xs font-mono">
          {event.author}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {new Date(event.timestamp * 1000).toLocaleTimeString()}
        </span>
      </div>
      {parts.map((part, i) => {
        if (part.text) {
          return (
            <p key={i} className="text-sm whitespace-pre-wrap leading-relaxed">
              {part.text}
            </p>
          );
        }
        if (part.functionCall) {
          return (
            <div key={i} className="mt-1 rounded bg-muted px-3 py-2 font-mono text-xs">
              <span className="text-primary font-semibold">
                {part.functionCall.name}
              </span>
              <span className="text-muted-foreground">
                ({summarizeArgs(part.functionCall.args)})
              </span>
            </div>
          );
        }
        if (part.functionResponse) {
          const status =
            (part.functionResponse.response as Record<string, unknown>)?.status;
          return (
            <div key={i} className="mt-1 rounded bg-muted/50 px-3 py-2 font-mono text-xs text-muted-foreground">
              {part.functionResponse.name} {"->"}{" "}
              {typeof status === "string" ? status : "done"}
            </div>
          );
        }
        return null;
      })}
    </div>
  );
}

export function EventLog({
  events,
  className,
}: {
  events: AgentEvent[];
  className?: string;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  return (
    <ScrollArea className={className}>
      <div className="divide-y divide-border">
        {events.length === 0 && (
          <p className="px-4 py-8 text-center text-sm text-muted-foreground">
            Waiting for events...
          </p>
        )}
        {events.map((event, i) => (
          <EventItem key={i} event={event} />
        ))}
        <div ref={bottomRef} />
      </div>
    </ScrollArea>
  );
}
