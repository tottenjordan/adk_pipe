"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import { SegmentedControl } from "@/components/segmented-control";
import { getRatings, putRating } from "@/lib/api";
import { SHORTCUTS } from "@/lib/shortcuts";
import { cn } from "@/lib/utils";
import type { Proof } from "@/lib/eval-matching";
import { FAIL_REASON_LABELS, failReasonsFor, type FailReason } from "@/lib/rating-reasons";
import {
  NOTE_MAX_CHARS,
  buildRatingPayload,
  canRateAdCopy,
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
import { useProofKeyTarget } from "./proof-keys";

const VERDICT_OPTIONS: { value: RatingVerdict | ""; label: string; keyshortcuts: string }[] = [
  { value: "pass", label: "Pass", keyshortcuts: SHORTCUTS.pass.aria },
  { value: "fail", label: "Fail", keyshortcuts: SHORTCUTS.fail.aria },
];
const SCORE_OPTIONS = [
  { value: "", label: "No score", keyshortcuts: SHORTCUTS.noScore.aria },
  ...[1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: String(n), keyshortcuts: String(n) })),
];

/** How long the "Saved" confirmation stays before it clears. */
export const SAVED_CLEAR_MS = 2500;

export type SetRating = (creativeKey: string, rating: Rating | undefined) => void;

/**
 * The caller's ratings for one run, keyed by creative. Loading fails soft: the
 * sheet just shows no "Rated" marks and the controls start empty.
 */
export function useSessionRatings(sessionId: string, enabled: boolean) {
  const [byKey, setByKey] = useState<Record<string, Rating>>({});
  // Another session starts empty, so the load merge below only ever sees this
  // session's local writes.
  const [forSession, setForSession] = useState(sessionId);
  if (forSession !== sessionId) {
    setForSession(sessionId);
    setByKey({});
  }

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    getRatings(sessionId)
      .then((list) => {
        // Merge, local writes winning: a rating saved (or its optimistic entry)
        // before this load returned is newer than the list.
        if (!cancelled) setByKey((prev) => ({ ...ratingsByKey(list), ...prev }));
      })
      .catch(() => {
        /* fail soft: rating still works, only the existing ones aren't shown */
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

  return { byKey, setRating };
}

/**
 * "Why did it fail?": a multi-select group of muted toggle chips (aria-pressed;
 * a selected chip also shows a check mark, so state isn't carried by colour alone).
 */
function FailReasonChips({
  kind,
  selected,
  onToggle,
  disabled,
  shown,
  instant,
}: {
  kind: RatingKind;
  selected: readonly FailReason[];
  onToggle: (reason: FailReason) => void;
  disabled: boolean;
  /** Only on a fail; otherwise the group keeps its space (no layout shift) but is invisible and inert. */
  shown: boolean;
  /** Skip the fade (the verdict was set by a key: key actions never animate). */
  instant: boolean;
}) {
  const id = useId();
  return (
    <div
      role="group"
      aria-labelledby={`${id}-label`}
      aria-hidden={!shown || undefined}
      inert={!shown}
      data-testid={`fail-reasons-${kind}`}
      className={cn(
        shown ? "opacity-100" : "opacity-0",
        !instant &&
          (shown
            ? "transition-opacity duration-(--dur-pop) ease-(--ease-out)"
            : "transition-opacity duration-(--dur-exit) ease-(--ease-out)")
      )}
    >
      <FieldLabel id={`${id}-label`}>Why did it fail? (optional)</FieldLabel>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {failReasonsFor(kind).map((reason) => {
          const on = selected.includes(reason);
          return (
            <button
              key={reason}
              type="button"
              aria-pressed={on}
              disabled={disabled}
              onClick={() => onToggle(reason)}
              className={cn(
                "inline-flex h-7 items-center gap-1 rounded-sm border px-2 text-xs transition-colors disabled:opacity-50",
                on
                  ? "border-foreground bg-muted font-medium text-foreground"
                  : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              {on && <Check className="size-3" aria-hidden="true" />}
              {FAIL_REASON_LABELS[reason]}
            </button>
          );
        })}
      </div>
    </div>
  );
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
 * One creative's human rating: pass/fail, optional fail-reason chips (on a fail),
 * optional 1–5 score and note, Save.
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
  // Follow `saved` (the ratings list arriving after the dialog opened, the server's
  // answer) only until the user edits; after that the draft is theirs, so a failed
  // save's revert never wipes what they typed.
  const [touched, setTouched] = useState(false);
  const [syncedFrom, setSyncedFrom] = useState(saved);
  if (!touched && saved !== syncedFrom) {
    setSyncedFrom(saved);
    setDraft(draftFrom(saved));
  }
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  // The last edit came from a key: the fail-reason group appears without a fade.
  const [viaKey, setViaKey] = useState(false);
  const subject = title.toLowerCase();
  const payload = buildRatingPayload(appName, creativeKey, kind, draft);
  const canSave = payload !== null && status !== "saving" && isDirty(draft, saved);
  const rootRef = useRef<HTMLDivElement>(null);
  // Key-driven edits switch the pressed option without the colour fade.
  const keyInstant = viaKey ? "[&>button]:transition-none" : undefined;

  // "Saved" is a passing confirmation: it clears on its own.
  useEffect(() => {
    if (status !== "saved") return;
    const timer = setTimeout(() => setStatus("idle"), SAVED_CLEAR_MS);
    return () => clearTimeout(timer);
  }, [status]);

  const edit = (patch: Partial<RatingDraft>, fromKey = false) => {
    setTouched(true);
    setViaKey(fromKey);
    // Reasons only belong to a fail: switching to Pass clears them.
    setDraft((d) => ({ ...d, ...patch, ...(patch.verdict === "pass" ? { failReasons: [] } : {}) }));
    if (status !== "saving") setStatus("idle");
  };

  const toggleReason = (reason: FailReason) =>
    edit({
      failReasons: draft.failReasons.includes(reason)
        ? draft.failReasons.filter((r) => r !== reason)
        : [...draft.failReasons, reason],
    });

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

  useProofKeyTarget({
    kind,
    root: rootRef,
    // The first group is the verdict.
    focus: () => rootRef.current?.querySelector<HTMLButtonElement>('[role="group"] button')?.focus(),
    handleKey: (key) => {
      if (status === "saving") return false;
      if (key === "p" || key === "f") edit({ verdict: key === "p" ? "pass" : "fail" }, true);
      else if (key === "0") edit({ score: null }, true);
      else if (/^[1-5]$/.test(key)) edit({ score: Number(key) }, true);
      else if (key === "s") {
        if (!canSave) return false;
        void save();
      } else return false;
      return true;
    },
  });

  return (
    <div ref={rootRef} className="space-y-2" data-testid={`rating-${kind}`}>
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
          className={keyInstant}
        />
        <SegmentedControl
          label={`Your score for the ${subject}, 1 to 5 (optional)`}
          options={SCORE_OPTIONS}
          value={draft.score === null ? "" : String(draft.score)}
          onChange={(v) => edit({ score: v === "" ? null : Number(v) })}
          disabled={status === "saving"}
          className={keyInstant}
        />
      </div>
      <FailReasonChips
        kind={kind}
        selected={draft.failReasons}
        onToggle={toggleReason}
        disabled={status === "saving"}
        shown={draft.verdict === "fail"}
        instant={viaKey}
      />
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
        <Button size="sm" onClick={save} disabled={!canSave} aria-keyshortcuts={SHORTCUTS.save.aria}>
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

/** One key cap (or cap range) and what it does, for the legend. */
function KeyHint({ caps, label }: { caps: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <Kbd>{caps}</Kbd>
      {label}
    </span>
  );
}

/**
 * The rating keys, from the shortcut registry. They rate the visual unless focus
 * is in the ad copy control (C moves it there, V back).
 */
function RatingKeysLegend({ copy }: { copy: boolean }) {
  const cap = (id: "pass" | "fail" | "noScore" | "save" | "rateCopy" | "rateVisual") =>
    SHORTCUTS[id].combos[0][0];
  const scores = SHORTCUTS.score.combos;
  return (
    <p
      data-testid="rating-keys"
      className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
    >
      <span className="sr-only">Keyboard: </span>
      <KeyHint caps={cap("pass")} label="pass" />
      <KeyHint caps={cap("fail")} label="fail" />
      <KeyHint caps={`${scores[0][0]}–${scores[scores.length - 1][0]}`} label="score" />
      <KeyHint caps={cap("noScore")} label="no score" />
      <KeyHint caps={cap("save")} label="save" />
      {copy && (
        <>
          <KeyHint caps={cap("rateCopy")} label="ad copy" />
          <KeyHint caps={cap("rateVisual")} label="visual" />
        </>
      )}
    </p>
  );
}

/**
 * The "Your rating" section of the proof-detail dialog: one control for the
 * paired ad copy (when there is one) and one for the visual. A copy paired only
 * by position can't be rated (a short reason is shown instead). Keyed per creative
 * only, so each control keeps its draft while the ratings list loads or saves.
 */
export function CreativeRatings({
  proof,
  appName,
  sessionId,
  byKey,
  onChange,
}: {
  proof: Proof;
  appName: string;
  sessionId: string;
  byKey: Record<string, Rating>;
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
        <RatingKeysLegend copy={keys.adCopy !== undefined} />
      </div>
      {keys.adCopy && (
        <RatingControl
          key={keys.adCopy}
          appName={appName}
          sessionId={sessionId}
          creativeKey={keys.adCopy}
          kind="ad_copy"
          title="Ad copy"
          saved={byKey[keys.adCopy]}
          onChange={onChange}
        />
      )}
      {proof.adCopy && !canRateAdCopy(proof) && (
        <div className="space-y-1" data-testid="rating-ad_copy">
          <FieldLabel as="h4">Ad copy</FieldLabel>
          <p className="text-xs text-muted-foreground">
            Can&apos;t rate this copy: it couldn&apos;t be matched to this concept reliably.
          </p>
        </div>
      )}
      <RatingControl
        key={keys.visual}
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
