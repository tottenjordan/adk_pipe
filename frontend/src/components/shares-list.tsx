"use client";

import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { copyStatus, copyText } from "@/components/share-dialog";
import { listShares, revokeShare } from "@/lib/api";
import { ShareError, shareErrorMessage, sharesForSession, type Share } from "@/lib/shares";

/** "Slate" / "1 creative" / "N creatives". */
export function scopeLabel(share: Share): string {
  if (share.scope === "slate") return "Slate";
  const n = share.concept_names.length || 1;
  return n === 1 ? "1 creative" : `${n} creatives`;
}

function createdLabel(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * "Shared links" for one run: the caller's active shares of this session
 * (newest first) with Copy and a confirmed Revoke. `created` holds links made
 * on this page since load (newest first) so they appear without a refetch.
 * Renders nothing until this session has a link (or the list fails to load).
 */
export function SessionShares({
  sessionId,
  created = [],
}: {
  sessionId: string;
  created?: Share[];
}) {
  const [fetched, setFetched] = useState<Share[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState<Share | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [copied, setCopied] = useState("");

  useEffect(() => {
    let cancelled = false;
    listShares()
      .then((list) => {
        if (!cancelled) setFetched(list);
      })
      .catch((err) => {
        if (!cancelled)
          setLoadError(
            `Shared links could not load. ${err instanceof ShareError ? err.message : ""}`.trim()
          );
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  const shares = useMemo(() => {
    const seen = new Set<string>();
    return sharesForSession([...created, ...fetched], sessionId).filter((s) => {
      if (seen.has(s.token) || removed.has(s.token)) return false;
      seen.add(s.token);
      return true;
    });
  }, [created, fetched, sessionId, removed]);

  const remove = (token: string) => setRemoved((prev) => new Set(prev).add(token));
  const setRowError = (token: string, message: string | null) =>
    setRowErrors((prev) => {
      const next = { ...prev };
      if (message) next[token] = message;
      else delete next[token];
      return next;
    });

  const revoke = async () => {
    const target = confirming;
    if (!target) return;
    setRevoking(true);
    try {
      await revokeShare(target.token);
      remove(target.token);
    } catch (err) {
      if (err instanceof ShareError && err.reason === "share_not_found") remove(target.token);
      else setRowError(target.token, err instanceof ShareError ? err.message : shareErrorMessage(null, 0));
    } finally {
      setRevoking(false);
      setConfirming(null);
    }
  };

  if (shares.length === 0 && !loadError) return null;

  return (
    <section aria-labelledby="shared-links-title" className="mb-4 rounded-lg border border-border bg-card px-4 py-3">
      <h2 id="shared-links-title" className="text-sm font-semibold text-foreground">
        Shared links
      </h2>
      {loadError && (
        <p role="alert" className="mt-1 text-sm text-mark-fail">
          {loadError}
        </p>
      )}
      {shares.length > 0 && (
        <ul aria-label="Shared links" className="mt-2 divide-y divide-border">
          {shares.map((s) => (
            <li key={s.token} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground">{s.title}</p>
                <p className="text-xs text-muted-foreground">
                  <span>{scopeLabel(s)}</span>
                  <span aria-hidden="true"> · </span>
                  <span>
                    Created <time dateTime={s.created_at}>{createdLabel(s.created_at)}</time>
                  </span>
                </p>
                {rowErrors[s.token] && (
                  <p role="alert" className="mt-0.5 text-xs text-mark-fail">
                    {rowErrors[s.token]}
                  </p>
                )}
              </div>
              {s.include_eval && <Badge variant="outline">With checks</Badge>}
              <Button
                variant="outline"
                size="sm"
                aria-label={`Copy link to ${s.title}`}
                onClick={async () => setCopied(copyStatus(await copyText(s.url)))}
              >
                Copy
              </Button>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Revoke ${s.title}`}
                onClick={() => {
                  setRowError(s.token, null);
                  setConfirming(s);
                }}
              >
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      <p data-testid="shares-copy-status" aria-live="polite" className="sr-only">
        {copied}
      </p>

      <Dialog
        open={confirming !== null}
        onOpenChange={(next) => {
          if (!next && !revoking) setConfirming(null);
        }}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Revoke this link?</DialogTitle>
            <DialogDescription>
              {confirming ? `"${confirming.title}" stops working for everyone who has it. ` : ""}
              This can&apos;t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(null)} disabled={revoking}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={revoke} disabled={revoking}>
              {revoking ? "Revoking…" : "Revoke link"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
