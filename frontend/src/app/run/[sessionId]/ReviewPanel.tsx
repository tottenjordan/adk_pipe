"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  buildConceptEdits,
  extractItems,
  parseRawGtrends,
  type ConceptDraft,
} from "./run-helpers";

const ASPECT_RATIO_OPTIONS = ["9:16", "1:1", "4:5", "3:4", "16:9"];

/* ── Review panel components for interactive mode ── */

/**
 * Cmd/Ctrl+Enter runs the panel's primary (approve) action from anywhere on
 * the page while the panel is shown. Skipped when `enabled` is false (e.g. no
 * trends selected yet), on key repeat, and when another handler already took it.
 */
function useApproveShortcut(onApprove: () => void, enabled = true) {
  const latest = useRef(onApprove);
  useEffect(() => {
    latest.current = onApprove;
  });
  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
      if (e.repeat || e.defaultPrevented) return;
      e.preventDefault();
      latest.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [enabled]);
}

/** Inline hint for the approve shortcut, shown next to the action buttons. */
function ShortcutHint({ action = "approve" }: { action?: string }) {
  const key = "rounded-sm border border-border bg-muted px-1 font-sans text-xs text-foreground";
  return (
    <span className="text-xs text-muted-foreground">
      <kbd className={key}>Ctrl</kbd> <kbd className={key}>Enter</kbd> or{" "}
      <kbd className={key}>⌘</kbd> <kbd className={key}>Enter</kbd> to {action}
    </span>
  );
}

const ACTIONS_ROW = "flex flex-wrap items-center gap-3";

function ReviewResearch({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const [editMode, setEditMode] = useState(false);
  const report = state.combined_final_cited_report as string | undefined;
  const [editedReport, setEditedReport] = useState(report ?? "");
  const approve = () =>
    onResume({ status: "approved", feedback, instruction: "User approved the research. Continue to the next step in the WORKFLOW." });
  useApproveShortcut(approve);

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
          <h2 className="text-lg font-semibold">Review research report</h2>
        </div>
        {report && (
          <button
            onClick={() => setEditMode((v) => !v)}
            className="rounded-sm text-xs font-medium text-primary hover:underline"
          >
            {editMode ? "Preview" : "Edit"}
          </button>
        )}
      </div>
      <p className="text-sm text-muted-foreground">
        Approve to continue to ad copy. To steer the next steps, add feedback
        and choose Request changes — the feedback is passed on; the research
        itself isn&apos;t re-run.
      </p>
      {report && !editMode && (
        <div className="max-h-[28rem] overflow-y-auto rounded-md bg-background p-5 border border-border prose prose-sm prose-neutral max-w-none
          prose-headings:text-foreground prose-headings:font-bold
          prose-h1:text-lg prose-h2:text-base prose-h3:text-sm
          prose-p:text-foreground/85 prose-p:leading-relaxed
          prose-a:text-primary prose-a:no-underline hover:prose-a:underline
          prose-strong:text-foreground prose-strong:font-semibold
          prose-li:text-foreground/85
          prose-code:text-xs prose-code:bg-muted prose-code:rounded prose-code:px-1
          prose-blockquote:border-l-primary/30 prose-blockquote:text-muted-foreground">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
        </div>
      )}
      {report && editMode && (
        <Textarea
          value={editedReport}
          onChange={(e) => setEditedReport(e.target.value)}
          rows={16}
          className="font-mono text-xs leading-relaxed max-h-[28rem]"
        />
      )}
      <FieldLabel as="label" htmlFor="review-research-feedback">
        Feedback (optional)
      </FieldLabel>
      <Textarea
        id="review-research-feedback"
        placeholder="e.g. lean on the nostalgia angle, skip the price comparison"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="-mt-2"
      />
      <div className={ACTIONS_ROW}>
        <Button onClick={approve}>
          Approve &amp; continue
        </Button>
        <Button
          variant="outline"
          onClick={() => onResume({ status: "revision_requested", feedback, instruction: "User requested changes to the research. Address their feedback, then continue the WORKFLOW." })}
          disabled={!feedback}
        >
          Request changes
        </Button>
        <ShortcutHint />
      </div>
    </div>
  );
}

/** Labeled field for review cards */
function ReviewField({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div>
      <FieldLabel as="dt">{label}</FieldLabel>
      <dd className="mt-0.5 text-sm leading-snug text-foreground/85">{value}</dd>
    </div>
  );
}

function ReviewAdCopies({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const adCopies = extractItems(state.ad_copy_critique);
  const approve = () =>
    onResume({ status: "approved", feedback, instruction: "User approved the ad copies. Continue to the next step in the WORKFLOW — generate visual concepts." });
  useApproveShortcut(approve);

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
        <h2 className="text-lg font-semibold">Review ad copies</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Approve to continue to visual concepts. To steer them, add feedback and
        choose Request changes — the feedback is passed on; the ad copy itself
        isn&apos;t rewritten.
      </p>
      {adCopies && (
        <div className="space-y-3 max-h-[28rem] overflow-y-auto">
          {adCopies.map((copy, i) => (
            <dl key={i} className="rounded-md border border-border bg-background p-4 space-y-3">
              {/* Title bar */}
              <div className="flex items-center gap-2 pb-2 border-b border-border">
                <Badge variant="secondary" className="text-xs px-1.5 py-0 bg-card text-foreground border border-border font-semibold tabular-nums">
                  {i + 1}
                </Badge>
                <span className="text-sm font-bold text-foreground">
                  {String(copy.headline ?? `Ad copy ${i + 1}`)}
                </span>
              </div>

              {/* Two-column field grid */}
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Body text" value={String(copy.body_text ?? "")} />
                </div>
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Tone / style" value={String(copy.tone_style ?? "")} />
                </div>
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Call to action" value={String(copy.call_to_action ?? "")} />
                </div>
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Trend connection" value={String(copy.trend_connection ?? "")} />
                </div>
              </div>

              {/* Full-width fields */}
              {!!copy.audience_appeal_rationale && (
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Audience appeal" value={String(copy.audience_appeal_rationale)} />
                </div>
              )}
              {!!copy.social_caption && (
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Social caption" value={String(copy.social_caption)} />
                </div>
              )}
              {!!copy.detailed_performance_rationale && (
                <div className="rounded-md bg-card px-3 py-2">
                  <ReviewField label="Performance rationale" value={String(copy.detailed_performance_rationale)} />
                </div>
              )}
            </dl>
          ))}
        </div>
      )}
      <FieldLabel as="label" htmlFor="review-ad-copies-feedback">
        Feedback (optional)
      </FieldLabel>
      <Textarea
        id="review-ad-copies-feedback"
        placeholder="e.g. make the calls to action less salesy"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="-mt-2"
      />
      <div className={ACTIONS_ROW}>
        <Button onClick={approve}>
          Approve &amp; continue
        </Button>
        <Button
          variant="outline"
          onClick={() => onResume({ status: "revision_requested", feedback, instruction: "User requested changes to the ad copies. Carry their feedback forward, then continue the WORKFLOW — generate visual concepts." })}
          disabled={!feedback}
        >
          Request changes
        </Button>
        <ShortcutHint />
      </div>
    </div>
  );
}

function ReviewVisualConcepts({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const concepts = useMemo(
    () => extractItems(state.final_visual_concepts) ?? [],
    [state.final_visual_concepts]
  );
  // Editable per-concept drafts, seeded once from the finalized concepts.
  const [drafts, setDrafts] = useState<ConceptDraft[]>(() =>
    concepts.map((c) => ({
      image_generation_prompt: String(c.image_generation_prompt ?? ""),
      aspect_ratio: String(c.aspect_ratio ?? ""),
      visual_style: String(c.visual_style ?? ""),
      revision_note: "",
    }))
  );

  const update = (i: number, field: keyof ConceptDraft, value: string) =>
    setDrafts((prev) =>
      prev.map((d, j) => (i === j ? { ...d, [field]: value } : d))
    );

  const submit = () => {
    const edits = buildConceptEdits(concepts, drafts);
    onResume({
      status: "approved",
      edits,
      instruction:
        "User reviewed the visual concepts. Continue to the next step in the WORKFLOW — apply any revision notes, then generate images.",
    });
  };
  useApproveShortcut(submit);

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
        <h2 className="text-lg font-semibold">Review visual concepts</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        To change a concept, edit its image prompt, aspect ratio or style, or
        add a revision note (applied by the AI before rendering). Approve to
        generate images. Changing the style label alone won&apos;t change the
        image unless the prompt or note reflects it.
      </p>
      {concepts.length > 0 && (
        <div className="space-y-3 max-h-[32rem] overflow-y-auto">
          {concepts.map((concept, i) => (
            <div key={i} className="rounded-md border border-border bg-background p-4 space-y-3">
              <div className="font-medium">
                {String(concept.concept_name ?? `Concept ${i + 1}`)}
              </div>
              <div className="text-sm text-muted-foreground">
                {String(concept.concept_summary ?? "")}
              </div>

              <FieldLabel as="label" htmlFor={`concept-${i}-prompt`}>
                Image prompt
              </FieldLabel>
              <Textarea
                id={`concept-${i}-prompt`}
                value={drafts[i]?.image_generation_prompt ?? ""}
                onChange={(e) =>
                  update(i, "image_generation_prompt", e.target.value)
                }
                rows={4}
                className="text-sm leading-relaxed"
              />

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <FieldLabel as="label" htmlFor={`concept-${i}-aspect`}>
                    Aspect ratio
                  </FieldLabel>
                  <select
                    id={`concept-${i}-aspect`}
                    value={drafts[i]?.aspect_ratio ?? ""}
                    onChange={(e) => update(i, "aspect_ratio", e.target.value)}
                    className="mt-1 w-full rounded-sm border border-input bg-card px-2 py-1.5 text-sm"
                  >
                    {/* Keep whatever the concept currently has, even if custom. */}
                    {drafts[i]?.aspect_ratio &&
                      !ASPECT_RATIO_OPTIONS.includes(drafts[i].aspect_ratio) && (
                        <option value={drafts[i].aspect_ratio}>
                          {drafts[i].aspect_ratio}
                        </option>
                      )}
                    {ASPECT_RATIO_OPTIONS.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <FieldLabel as="label" htmlFor={`concept-${i}-style`}>
                    Style
                  </FieldLabel>
                  <Input
                    id={`concept-${i}-style`}
                    value={drafts[i]?.visual_style ?? ""}
                    onChange={(e) => update(i, "visual_style", e.target.value)}
                    className="mt-1 text-sm"
                  />
                </div>
              </div>

              <FieldLabel as="label" htmlFor={`concept-${i}-note`}>
                Revision note (applied by AI)
              </FieldLabel>
              <Input
                id={`concept-${i}-note`}
                placeholder="e.g., make the background brighter, add a dog"
                value={drafts[i]?.revision_note ?? ""}
                onChange={(e) => update(i, "revision_note", e.target.value)}
                className="text-sm"
              />
            </div>
          ))}
        </div>
      )}
      <div className={ACTIONS_ROW}>
        <Button onClick={submit}>Approve &amp; generate images</Button>
        <ShortcutHint />
      </div>
    </div>
  );
}

function ReviewTrends({
  state,
  onResume,
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  const candidates = useMemo(
    () => parseRawGtrends(state.raw_gtrends),
    [state.raw_gtrends]
  );
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [instruction, setInstruction] = useState("");

  const confirm = () =>
    onResume({
      status: "selected",
      selected_trends: [...selected],
      instruction,
    });
  useApproveShortcut(confirm, selected.size > 0);

  const toggle = (term: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(term)) next.delete(term);
      else next.add(term);
      return next;
    });
  };

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
          <h2 className="text-lg font-semibold">Pick your trends</h2>
        </div>
        {candidates.length > 0 && (
          <Badge
            variant="secondary"
            className="text-xs px-1.5 py-0 font-semibold tabular-nums"
          >
            {selected.size} / {candidates.length} selected
          </Badge>
        )}
      </div>
      <p className="text-sm text-muted-foreground">
        Select the trends you want to keep. The agent will skip its automatic
        pick and research the trends you choose.
      </p>
      {candidates.length === 0 ? (
        <p className="text-sm text-muted-foreground italic">
          No candidate trends available yet.
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 max-h-[28rem] overflow-y-auto">
          {candidates.map((term) => {
            const isSelected = selected.has(term);
            return (
              <button
                key={term}
                type="button"
                onClick={() => toggle(term)}
                aria-pressed={isSelected}
                className={`rounded-md border px-4 py-3 text-left text-sm transition-colors ${
                  isSelected
                    ? "border-primary bg-primary/10 text-primary font-semibold"
                    : "border-border bg-background text-foreground hover:border-primary/40"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border text-[10px] font-bold ${
                      isSelected
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-border text-transparent"
                    }`}
                  >
                    &#10003;
                  </span>
                  <span className="leading-snug break-words">{term}</span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      <FieldLabel as="label" htmlFor="review-trends-note">
        Note for the agent (optional)
      </FieldLabel>
      <Textarea
        id="review-trends-note"
        placeholder="e.g. focus on music and live events"
        value={instruction}
        onChange={(e) => setInstruction(e.target.value)}
        rows={2}
        className="-mt-2"
      />
      <div className={ACTIONS_ROW}>
        <Button disabled={selected.size === 0} onClick={confirm}>
          Confirm selection
        </Button>
        {selected.size > 0 ? (
          <ShortcutHint action="confirm" />
        ) : (
          <span className="text-xs text-muted-foreground">Select at least one trend.</span>
        )}
      </div>
    </div>
  );
}

export function ReviewPanel({
  functionName,
  sessionState,
  onResume,
}: {
  functionName: string;
  sessionState: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
}) {
  // Move focus to the panel when a checkpoint appears so keyboard and screen
  // reader users land on the review instead of wherever they were.
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, [functionName]);

  let panel: React.ReactNode = null;
  if (functionName === "review_research") {
    panel = <ReviewResearch state={sessionState} onResume={onResume} />;
  } else if (functionName === "review_ad_copies") {
    panel = <ReviewAdCopies state={sessionState} onResume={onResume} />;
  } else if (functionName === "review_visual_concepts") {
    panel = <ReviewVisualConcepts state={sessionState} onResume={onResume} />;
  } else if (functionName === "review_trends") {
    panel = <ReviewTrends state={sessionState} onResume={onResume} />;
  }
  if (!panel) return null;
  return (
    <section ref={ref} tabIndex={-1} aria-label="Review" className="outline-none">
      {panel}
    </section>
  );
}
