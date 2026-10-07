"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import { SegmentedControl } from "@/components/segmented-control";
import { getRatings, putRating } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Proof } from "@/lib/eval-matching";
import {
  NOTE_MAX_CHARS,
  buildRatingPayload,
  creativeKeysFor,
  draftFrom,
  isDirty,
  optimisticRating,
  ratingsByKey,
  type Rating,
  type RatingDraft,
  type RatingKind,
  type RatingVerdict,
} from "@/lib/ratings";

const VERDICT_OPTIONS: { value: RatingVerdict | ""; label: string }[] = [
  { value: "pass", label: "Pass" },
  { value: "fail", label: "Fail" },
];
const SCORE_OPTIONS = [
  { value: "", label: "No score" },
  ...[1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: String(n) })),
];

export type SetRating = (creativeKey: string, rating: Rating | undefined) => void;

/**
 * The caller's ratings for one run, keyed by creative. Loading fails soft: the
 * sheet just shows no "Rated" marks and the controls start empty.
 */
export function useSessionRatings(sessionId: string, enabled: boolean) {
  const [byKey, setByKey] = useState<Record<string, Rating>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    getRatings(sessionId)
      .then((list) => {
        if (!cancelled) setByKey(ratingsByKey(list));
      })
      .catch(() => {
        /* fail soft: rating still works, only the existing ones aren't shown */
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, enabled]);

  const setRating: SetRating = useCallback((creativeKey, rating) => {
    setByKey((prev) => {
      const next = { ...prev };
      if (rating) next[creativeKey] = rating;
      else delete next[creativeKey];
      return next;
    });
  }, []);

  return { byKey, loaded, setRating };
}

function SavedSummary({ rating }: { rating?: Rating }) {
  if (!rating) return <span className="text-xs text-muted-foreground">Not rated yet</span>;
  const pass = rating.verdict === "pass";
  return (
    <span className="text-xs text-muted-foreground">
      Saved:{" "}
      <span className={cn("font-medium", pass ? "text-mark-pass" : "text-mark-fail")}>
        {pass ? "pass" : "fail"}
      </span>
      {rating.score !== null && rating.score !== undefined ? `, ${rating.score}/5` : ""}
    </span>
  );
}

/**
 * One creative's human rating: pass/fail, optional 1–5 score and note, Save.
 * Save is optimistic (the sheet's "Rated" mark appears at once) and reverts to
 * the previous rating if the request fails; the draft is kept so the user can retry.
 */
export function RatingControl({
  appName,
  sessionId,
  creativeKey,
  kind,
  title,
  saved,
  onChange,
}: {
  appName: string;
  sessionId: string;
  creativeKey: string;
  kind: RatingKind;
  /** e.g. "Ad copy" / "Visual". */
  title: string;
  saved?: Rating;
  onChange: SetRating;
}) {
  const id = useId();
  const [draft, setDraft] = useState<RatingDraft>(() => draftFrom(saved));
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const subject = title.toLowerCase();
  const payload = buildRatingPayload(appName, creativeKey, kind, draft);
  const canSave = payload !== null && status !== "saving" && isDirty(draft, saved);

  const edit = (patch: Partial<RatingDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    if (status !== "saving") setStatus("idle");
  };

  const save = async () => {
    if (!payload) return;
    const previous = saved;
    const optimistic = optimisticRating(payload, previous);
    onChange(creativeKey, optimistic);
    setStatus("saving");
    try {
      const stored = await putRating(sessionId, payload);
      onChange(creativeKey, { ...optimistic, ...stored });
      setStatus("saved");
    } catch {
      onChange(creativeKey, previous);
      setStatus("error");
    }
  };

  return (
    <div className="space-y-2" data-testid={`rating-${kind}`}>
      <div className="flex items-baseline justify-between gap-3">
        <FieldLabel as="h4" id={`${id}-title`}>
          {title}
        </FieldLabel>
        <SavedSummary rating={saved} />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <SegmentedControl
          label={`Your verdict on the ${subject}`}
          options={VERDICT_OPTIONS}
          value={draft.verdict}
          onChange={(v) => edit({ verdict: v })}
          disabled={status === "saving"}
        />
        <SegmentedControl
          label={`Your score for the ${subject}, 1 to 5 (optional)`}
          options={SCORE_OPTIONS}
          value={draft.score === null ? "" : String(draft.score)}
          onChange={(v) => edit({ score: v === "" ? null : Number(v) })}
          disabled={status === "saving"}
        />
      </div>
      <div>
        <FieldLabel as="label" htmlFor={`${id}-note`}>
          Note (optional)
        </FieldLabel>
        <Textarea
          id={`${id}-note`}
          value={draft.note}
          maxLength={NOTE_MAX_CHARS}
          onChange={(e) => edit({ note: e.target.value })}
          disabled={status === "saving"}
          className="mt-1 min-h-12"
          placeholder="What made it work, or not?"
        />
      </div>
      <div className="flex items-center gap-3">
        <Button size="sm" onClick={save} disabled={!canSave}>
          {status === "saving" ? "Saving…" : `Save ${subject} rating`}
        </Button>
        <span aria-live="polite" className="text-xs">
          {status === "saved" && <span className="text-muted-foreground">Saved</span>}
          {status === "error" && (
            <span className="text-mark-fail">Couldn&apos;t save your rating. Try again.</span>
          )}
        </span>
      </div>
    </div>
  );
}

/**
 * The "Your rating" section of the proof-detail dialog: one control for the
 * paired ad copy (when there is one) and one for the visual. Keyed per creative
 * (and on the first ratings load) so each starts from its saved rating.
 */
export function CreativeRatings({
  proof,
  appName,
  sessionId,
  byKey,
  loaded,
  onChange,
}: {
  proof: Proof;
  appName: string;
  sessionId: string;
  byKey: Record<string, Rating>;
  loaded: boolean;
  onChange: SetRating;
}) {
  const keys = creativeKeysFor(proof);
  return (
    <section className="space-y-4 border-t border-border pt-4" aria-label="Your rating">
      <div>
        <h3 className="text-sm font-semibold text-foreground">Your rating</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Your verdicts are compared with the judge&apos;s to check how far its scores can be trusted.
        </p>
      </div>
      {keys.adCopy && (
        <RatingControl
          key={`${keys.adCopy}:${loaded}`}
          appName={appName}
          sessionId={sessionId}
          creativeKey={keys.adCopy}
          kind="ad_copy"
          title="Ad copy"
          saved={byKey[keys.adCopy]}
          onChange={onChange}
        />
      )}
      <RatingControl
        key={`${keys.visual}:${loaded}`}
        appName={appName}
        sessionId={sessionId}
        creativeKey={keys.visual}
        kind="visual"
        title="Visual"
        saved={byKey[keys.visual]}
        onChange={onChange}
      />
    </section>
  );
}
