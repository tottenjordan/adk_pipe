"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { ChevronDownIcon } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { FieldLabel } from "@/components/field-label";
import { GcsWidget } from "@/components/gcs-widget";
import { cn } from "@/lib/utils";

type Field = { key: string; label: string; value: string };

function FieldGrid({ fields, breakAll }: { fields: Field[]; breakAll?: boolean }) {
  return (
    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
      {fields.map((f) => (
        <div key={f.key} className="min-w-0">
          <FieldLabel as="dt">{f.label}</FieldLabel>
          <dd
            className={cn(
              "mt-0.5 text-sm leading-snug break-words text-foreground",
              breakAll && "break-all"
            )}
          >
            {f.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Title, one-line campaign summary, actions, and an expandable panel with the
 * full campaign fields, visual direction and output folder.
 */
export function ResultsHeader({
  appName,
  sessionId,
  summary,
  campaignFields,
  visualDirectionFields,
  gcsUri,
  galleryUrl,
  actions,
}: {
  appName: string;
  sessionId: string;
  summary: string;
  campaignFields: Field[];
  visualDirectionFields: Field[];
  gcsUri: string;
  galleryUrl: string | null;
  /** Extra header actions, before the gallery link (e.g. "Share slate"). */
  actions?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const hasDetails = campaignFields.length > 0 || visualDirectionFields.length > 0 || !!gcsUri;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mb-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-foreground">Results</h1>
          <p className="mt-1 text-sm text-foreground/85">
            {summary || <span className="font-mono text-muted-foreground">{appName} / {sessionId}</span>}
            {hasDetails && (
              <CollapsibleTrigger className="ml-2 inline-flex items-center gap-0.5 rounded-sm text-sm font-medium text-primary hover:underline">
                {open ? "Hide details" : "Campaign details"}
                <ChevronDownIcon
                  aria-hidden="true"
                  className={cn("size-3.5 transition-transform", open && "rotate-180")}
                />
              </CollapsibleTrigger>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {actions}
          {galleryUrl && (
            <a
              href={galleryUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={buttonVariants({ variant: "outline", size: "sm" })}
            >
              Open portfolio gallery
            </a>
          )}
          <Link href="/" className={buttonVariants({ size: "sm" })}>
            New run
          </Link>
        </div>
      </div>

      {hasDetails && (
        <CollapsibleContent>
          <div className="mt-3 space-y-4 rounded-lg border border-border bg-card px-4 py-4">
            {campaignFields.length > 0 && <FieldGrid fields={campaignFields} />}
            {visualDirectionFields.length > 0 && (
              <div className="border-t border-border pt-4">
                <h2 className="mb-2 text-sm font-semibold text-foreground">Visual direction</h2>
                <FieldGrid fields={visualDirectionFields} breakAll />
              </div>
            )}
            <div className="flex flex-wrap items-start gap-4 border-t border-border pt-4">
              {gcsUri && <GcsWidget uri={gcsUri} />}
              <div>
                <FieldLabel as="p">Session</FieldLabel>
                <p className="mt-0.5 font-mono text-xs text-foreground/85">
                  {appName} / {sessionId}
                </p>
              </div>
            </div>
          </div>
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}
