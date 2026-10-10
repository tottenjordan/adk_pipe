"use client";

import { useState, type KeyboardEvent, type ReactNode } from "react";
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { FieldLabel } from "@/components/field-label";
import { cn } from "@/lib/utils";
import { dimensionLabel } from "@/lib/eval-dimensions";
import {
  scoreGates,
  type Casting,
  type CreativeScore,
  type ImageCheck,
  type Proof,
} from "@/lib/eval-matching";
import { ChecksList } from "./eval-checks";
import { ProofImage } from "./proof-grid";
import { CONDENSED, ScoreMark } from "./score-mark";

function TextField({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div>
      <FieldLabel as="dt">{label}</FieldLabel>
      <dd className="mt-0.5 text-sm leading-snug text-foreground">{value}</dd>
    </div>
  );
}

function ShortList({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <FieldLabel as="h4">{title}</FieldLabel>
      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs leading-snug text-foreground/85">
        {items.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * One eval section: overall mark, the binary checks (gated reports only),
 * a row per dimension (human label, bar, score/10 in pass/fail colour) —
 * headed "Quality (advisory)" when checks are shown — rationale behind
 * "Show reasoning", then strengths and improvements.
 */
function ScoreSection({
  title,
  score,
  notes,
  checksNote,
}: {
  title: string;
  score: CreativeScore;
  /** Extra context shown with the reasoning (e.g. the visual concept notes). */
  notes?: { label: string; value: string }[];
  /** Context line under the checks. */
  checksNote?: string;
}) {
  const gated = scoreGates(score).length > 0;
  const [showReasoning, setShowReasoning] = useState(false);
  return (
    <section className="border-t border-border pt-4">
      <div className="mb-3 flex items-end justify-between gap-3">
        <ScoreMark label={title} score={score} />
        <Button
          variant="ghost"
          size="xs"
          aria-expanded={showReasoning}
          onClick={() => setShowReasoning((v) => !v)}
          className="text-primary"
        >
          {showReasoning ? "Hide reasoning" : "Show reasoning"}
        </Button>
      </div>

      <ChecksList score={score} note={checksNote} />
      {gated && score.verdicts.length > 0 && (
        <FieldLabel as="h4" className="mb-1.5">
          Quality (advisory)
        </FieldLabel>
      )}

      <ul className="space-y-1.5">
        {score.verdicts.map((v) => {
          const pass = v.verdict === "pass";
          const clamped = Math.max(0, Math.min(10, v.score));
          return (
            <li key={v.dimension}>
              <div className="grid grid-cols-[8.5rem_minmax(0,1fr)_2.75rem] items-center gap-3">
                <span className="truncate text-xs text-foreground">
                  {dimensionLabel(v.dimension)}
                </span>
                <span className="h-1.5 bg-muted" aria-hidden="true">
                  <span
                    className={cn(
                      "block h-full",
                      pass ? "bg-mark-pass" : "bg-mark-fail"
                    )}
                    style={{ width: `${clamped * 10}%` }}
                  />
                </span>
                <span
                  className={cn(
                    "text-right text-xs font-semibold tabular-nums",
                    pass ? "text-mark-pass" : "text-mark-fail"
                  )}
                >
                  {v.score}/10
                  <span className="sr-only">{pass ? ", pass" : ", fail"}</span>
                </span>
              </div>
              {showReasoning && v.rationale && (
                <p className="mt-0.5 mb-1.5 text-xs leading-snug text-muted-foreground">
                  {v.rationale}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {showReasoning && notes && notes.some((n) => n.value) && (
        <dl className="mt-3 space-y-2 rounded-sm bg-muted/60 p-3">
          {notes.map((n) => (
            <TextField key={n.label} label={n.label} value={n.value} />
          ))}
        </dl>
      )}

      {(score.strengths.length > 0 || score.improvements.length > 0) && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <ShortList title="Strengths" items={score.strengths} />
          <ShortList title="Improvements" items={score.improvements} />
        </div>
      )}
    </section>
  );
}

/**
 * The post-render image check: a pass/fail mark (word spelled out, not colour
 * alone), the issues still open on the kept image, and a re-render note.
 * Absent check (QA off or unavailable) → nothing.
 */
export function ImageCheckSection({ check }: { check?: ImageCheck }) {
  if (!check) return null;
  const rerendered =
    check.rerenders === 1
      ? "Re-rendered once."
      : check.rerenders > 1
        ? `Re-rendered ${check.rerenders} times.`
        : null;
  return (
    <section className="border-t border-border pt-4">
      <div className="flex items-baseline gap-2">
        <FieldLabel as="h4">Image check</FieldLabel>
        <span
          className={cn(
            "text-xs font-medium",
            check.passed ? "text-mark-pass" : "text-mark-fail"
          )}
        >
          {check.passed ? "passed" : "issues"}
        </span>
      </div>
      {!check.passed && check.issues.length > 0 && (
        <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-xs leading-snug text-foreground/85">
          {check.issues.map((issue, i) => (
            <li key={i}>{issue}</li>
          ))}
        </ul>
      )}
      {rerendered && (
        <p className="mt-1.5 text-xs text-muted-foreground">{rerendered}</p>
      )}
    </section>
  );
}

/**
 * Person casting: "Cast: Yes — <reason>" for a concept rendered with the run's
 * consented person, or a note when the person photo was rejected at render time
 * (the image was then made without the person). Nothing otherwise.
 */
export function CastingSection({ casting }: { casting?: Casting }) {
  if (!casting) return null;
  if (casting.rejected) {
    const why =
      casting.rejected === "photo_unavailable"
        ? "Person photo unavailable"
        : "Person photo rejected by the safety filter";
    return (
      <p className="border-t border-border pt-4 text-xs text-muted-foreground">
        {`${why}; this image was made without the person.`}
      </p>
    );
  }
  return (
    <section className="border-t border-border pt-4">
      <dl>
        <TextField
          label="Cast"
          value={casting.reason ? `Yes — ${casting.reason}` : "Yes"}
        />
      </dl>
    </section>
  );
}

/**
 * Full view of one proof in a dialog: large image left, copy and scores right.
 * Left/Right arrows and the prev/next buttons step through the sheet in its
 * current sort order; Escape closes and focus returns to the grid item.
 */
export function ProofDetail({
  proofs,
  open,
  index,
  onIndexChange,
  onClose,
  imageUrlFor,
  returnFocusTo,
  ratingSlot,
  shareSlot,
  personaliseSlot,
}: {
  /** The grid's current (sorted) order. */
  proofs: Proof[];
  open: boolean;
  /** Pipeline index of the shown proof (kept after close so focus can return). */
  index: number | null;
  onIndexChange: (index: number) => void;
  onClose: () => void;
  imageUrlFor: (conceptName: string) => string | null;
  returnFocusTo: (index: number) => HTMLElement | null;
  /** Extra section rendered under the scores (the human rating control). */
  ratingSlot?: (proof: Proof) => ReactNode;
  /** Share action rendered next to the rating slot ("Share this creative"). */
  shareSlot?: (proof: Proof) => ReactNode;
  /** Personalised preview panel, under the rating slot (uncast creatives only). */
  personaliseSlot?: (proof: Proof) => ReactNode;
}) {
  const pos = index === null ? -1 : proofs.findIndex((p) => p.index === index);
  const proof = pos >= 0 ? proofs[pos] : undefined;
  const hasPrev = pos > 0;
  const hasNext = pos >= 0 && pos < proofs.length - 1;

  const go = (delta: number) => {
    const next = proofs[pos + delta];
    if (next) onIndexChange(next.index);
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.target instanceof HTMLElement && e.target.closest("input, textarea, select")) return;
    if (e.key === "ArrowLeft" && hasPrev) {
      e.preventDefault();
      go(-1);
    } else if (e.key === "ArrowRight" && hasNext) {
      e.preventDefault();
      go(1);
    }
  };

  const vc = proof?.concept;
  const ac = proof?.adCopy;
  const shareAction = proof ? shareSlot?.(proof) : null;

  return (
    <Dialog
      open={open && proof !== undefined}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {proof && vc && (
        <DialogContent
          onKeyDown={onKeyDown}
          finalFocus={() => returnFocusTo(proof.index)}
          className="flex max-h-[calc(100dvh-2rem)] w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-[min(72rem,calc(100%-2rem))] md:grid md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] data-open:animate-none data-closed:animate-none"
        >
          <div className="flex h-[38dvh] shrink-0 items-center justify-center bg-foreground/[0.04] md:h-[min(calc(100dvh-2rem),52rem)]">
            <ProofImage
              src={imageUrlFor(vc.concept_name)}
              alt={vc.concept_summary}
              cast={proof.casting?.cast === true}
              fit="contain"
              className="h-full w-full"
            />
          </div>

          <div className="flex min-h-0 flex-1 flex-col md:h-[min(calc(100dvh-2rem),52rem)]">
            <div className="flex items-center gap-1 border-b border-border px-4 py-2 pr-12">
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => go(-1)}
                disabled={!hasPrev}
                aria-label="Previous creative"
              >
                <ChevronLeftIcon />
              </Button>
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => go(1)}
                disabled={!hasNext}
                aria-label="Next creative"
              >
                <ChevronRightIcon />
              </Button>
              <span className="ml-2 text-xs text-muted-foreground tabular-nums">
                {pos + 1} of {proofs.length}
              </span>
            </div>

            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
              <div>
                <DialogTitle
                  className={cn(CONDENSED, "font-sans text-3xl leading-[1.05] text-foreground")}
                >
                  {vc.headline}
                </DialogTitle>
                <DialogDescription className="mt-1 text-xs">
                  {vc.concept_name}
                  {vc.visual_style ? `, ${vc.visual_style}` : ""}
                </DialogDescription>
              </div>

              {ac?.body_text && (
                <p className="text-sm leading-relaxed text-foreground">{ac.body_text}</p>
              )}

              <dl className="grid gap-3 sm:grid-cols-2">
                <TextField label="Call to action" value={ac?.call_to_action || vc.call_to_action} />
                <TextField label="Tone" value={ac?.tone_style || proof.adCopyEval?.tone_style} />
                <div className="sm:col-span-2">
                  <TextField label="Social caption" value={vc.social_caption || ac?.social_caption} />
                </div>
              </dl>

              {proof.adCopyEval && (
                <ScoreSection title="Ad copy" score={proof.adCopyEval.score} />
              )}
              {proof.visualEval && (
                <ScoreSection
                  title="Visual"
                  score={proof.visualEval.score}
                  checksNote={
                    proof.visualEval.image_judged === false
                      ? "Judged from the image prompt (the rendered image was not judged)."
                      : undefined
                  }
                  notes={[
                    { label: "Concept", value: vc.concept_summary },
                    { label: "Trend reference", value: vc.trend_reference },
                    { label: "Markets product", value: vc.markets_product },
                    { label: "Audience appeal", value: vc.audience_appeal },
                  ]}
                />
              )}
              <ImageCheckSection check={proof.imageCheck} />
              <CastingSection casting={proof.casting} />
              {ratingSlot?.(proof)}
              {personaliseSlot?.(proof)}
              {shareAction && (
                <div className="flex justify-end border-t border-border pt-4">{shareAction}</div>
              )}
              {!proof.adCopyEval && !proof.visualEval && (
                <p className="border-t border-border pt-4 text-xs text-muted-foreground">
                  No evaluation data for this creative.
                </p>
              )}
            </div>
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}
