"use client";

import { useId, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { createShare } from "@/lib/api";
import { isAbsoluteUrl, ShareError, shareErrorMessage, type Share } from "@/lib/shares";

export const SHARE_NOTICE =
  "Anyone with the link can view these creatives and copy. Prompts, notes and ratings are never shared.";

/** Copy `text` to the clipboard; resolves false when the browser refuses. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** "Link copied" / failure text for a copy attempt (shown in a polite live region). */
export const copyStatus = (ok: boolean) =>
  ok ? "Link copied" : "Couldn't copy. Select the link and copy it.";

/**
 * Freeze the slate (no `conceptNames`) or one creative into a public, revocable
 * link (runserver/shares.py). Each open starts fresh; focus returns to the
 * trigger on close.
 */
export function ShareDialog({
  appName,
  sessionId,
  conceptNames,
  triggerLabel,
  onCreated,
}: {
  appName: string;
  sessionId: string;
  /** Share just these creatives; omit for the whole slate. */
  conceptNames?: string[];
  triggerLabel: string;
  onCreated?: (share: Share) => void;
}) {
  const [open, setOpen] = useState(false);
  const [includeEval, setIncludeEval] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [share, setShare] = useState<Share | null>(null);
  const [copied, setCopied] = useState("");
  const single = Boolean(conceptNames?.length);
  const id = useId();
  const checkboxId = `${id}-eval`;
  const urlId = `${id}-url`;

  const onOpenChange = (next: boolean) => {
    if (next) {
      setIncludeEval(false);
      setBusy(false);
      setError(null);
      setShare(null);
      setCopied("");
    }
    setOpen(next);
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const created = await createShare(appName, sessionId, {
        ...(single ? { concept_names: conceptNames } : {}),
        include_eval: includeEval,
      });
      setShare(created);
      onCreated?.(created);
    } catch (err) {
      setError(err instanceof ShareError ? err.message : shareErrorMessage(null, 0));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (share) setCopied(copyStatus(await copyText(share.url)));
  };

  // Keep arrow keys from reaching an enclosing proof dialog (it pages creatives on them).
  const stopArrows = (e: KeyboardEvent) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") e.stopPropagation();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger render={<Button variant="outline" size="sm" />}>{triggerLabel}</DialogTrigger>
      <DialogContent onKeyDown={stopArrows} className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{single ? "Share this creative" : "Share slate"}</DialogTitle>
          <DialogDescription>{SHARE_NOTICE}</DialogDescription>
        </DialogHeader>

        {share ? (
          <div className="space-y-2">
            <label htmlFor={urlId} className="text-xs font-medium text-foreground">
              Link
            </label>
            <div className="flex gap-2">
              <input
                id={urlId}
                readOnly
                value={share.url}
                onFocus={(e) => e.currentTarget.select()}
                className="h-7 min-w-0 flex-1 rounded-sm border border-border bg-background px-2 font-mono text-xs text-foreground"
              />
              <Button variant="outline" size="sm" onClick={copy} aria-label="Copy link">
                Copy
              </Button>
              <a
                href={share.url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex h-7 items-center rounded-sm border border-border bg-card px-2.5 text-[0.8rem] font-medium hover:bg-muted"
              >
                Open<span className="sr-only"> (opens in a new tab)</span>
              </a>
            </div>
            {!isAbsoluteUrl(share.url) && (
              <p className="text-xs text-muted-foreground">
                Share service URL not configured, so this link is relative. Prefix it with the share
                service address before sending it.
              </p>
            )}
            {share.include_eval && (
              <p className="text-xs text-muted-foreground">Includes judge checks and scores.</p>
            )}
          </div>
        ) : (
          <label
            htmlFor={checkboxId}
            className="flex cursor-pointer items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5"
          >
            <input
              id={checkboxId}
              type="checkbox"
              checked={includeEval}
              onChange={(e) => setIncludeEval(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded-sm border-border accent-primary"
            />
            <span className="text-sm text-foreground">Include judge checks and scores</span>
          </label>
        )}

        {error && (
          <p role="alert" className="text-sm text-mark-fail">
            {error}
          </p>
        )}
        <p data-testid="share-copy-status" aria-live="polite" className="text-xs text-muted-foreground">
          {copied}
        </p>

        {!share && (
          <DialogFooter>
            <Button onClick={create} disabled={busy}>
              {busy ? "Creating link…" : "Create link"}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
