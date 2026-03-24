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

/** Map agent name to a colored dot */
function agentColor(author: string): string {
  if (author.includes("trend")) return "bg-emerald-400";
  if (author.includes("creative") || author.includes("composer")) return "bg-purple-400";
  if (author.includes("search") || author.includes("research")) return "bg-sky-400";
  if (author.includes("evaluator") || author.includes("critic")) return "bg-amber-400";
  if (author.includes("planner")) return "bg-pink-400";
  return "bg-blue-400";
}

function EventItem({ event, isLast }: { event: AgentEvent; isLast: boolean }) {
  const parts = event.content?.parts || [];
  const color = agentColor(event.author || "");

  return (
    <div className="relative pl-8 pb-4 last:pb-2">
      {/* Vertical connector line */}
      {!isLast && <div className="timeline-line" />}

      {/* Timeline dot */}
      <div
        className={`absolute left-0 top-1.5 h-6 w-6 rounded-full flex items-center justify-center ring-4 ring-background ${color}/20`}
      >
        <span className={`h-2.5 w-2.5 rounded-full ${color}`} />
      </div>

      {/* Content */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <Badge
            variant="outline"
            className="text-[10px] font-mono border-white/10 bg-white/5"
          >
            {event.author}
          </Badge>
          <span className="text-[10px] text-muted-foreground tabular-nums">
            {new Date(event.timestamp * 1000).toLocaleTimeString()}
          </span>
        </div>

        {parts.map((part, i) => {
          if (part.text) {
            return (
              <p
                key={i}
                className="text-sm whitespace-pre-wrap leading-relaxed text-foreground/90"
              >
                {part.text}
              </p>
            );
          }
          if (part.functionCall) {
            return (
              <div
                key={i}
                className="mt-1 rounded-lg bg-white/5 border border-white/5 px-3 py-2 font-mono text-xs"
              >
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
              (part.functionResponse.response as Record<string, unknown>)
                ?.status;
            return (
              <div
                key={i}
                className="mt-1 rounded-lg bg-white/3 border border-white/5 px-3 py-2 font-mono text-xs text-muted-foreground"
              >
                {part.functionResponse.name} {"->"}
                {" "}
                {typeof status === "string" ? status : "done"}
              </div>
            );
          }
          return null;
        })}
      </div>
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
      <div className="p-4">
        {events.length === 0 && (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <div className="mb-3 flex space-x-1">
              <div className="h-2 w-2 rounded-full bg-blue-400 animate-bounce" style={{ animationDelay: "0ms" }} />
              <div className="h-2 w-2 rounded-full bg-purple-400 animate-bounce" style={{ animationDelay: "150ms" }} />
              <div className="h-2 w-2 rounded-full bg-emerald-400 animate-bounce" style={{ animationDelay: "300ms" }} />
            </div>
            <p className="text-sm">Waiting for events...</p>
          </div>
        )}
        {events.map((event, i) => (
          <EventItem
            key={i}
            event={event}
            isLast={i === events.length - 1}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </ScrollArea>
  );
}
