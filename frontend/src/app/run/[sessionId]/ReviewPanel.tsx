"use client";

import { useState, useMemo } from "react";
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
        Review the research findings below. Approve to continue to ad copy generation,
        or provide feedback for revisions.
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
      <Textarea
        placeholder="Optional feedback or revision requests..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the research. Continue to the next step in the WORKFLOW." })}>
          Approve &amp; continue
        </Button>
        <Button
          variant="outline"
          onClick={() => onResume({ status: "revision_requested", feedback, instruction: "User requested changes to the research. Address their feedback, then continue the WORKFLOW." })}
          disabled={!feedback}
        >
          Request changes
        </Button>
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

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
        <h2 className="text-lg font-semibold">Review ad copies</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Review the generated ad copies. Approve to continue to visual concept generation.
      </p>
      {adCopies && (
        <div className="space-y-3 max-h-[28rem] overflow-y-auto">
          {adCopies.map((copy, i) => (
            <dl key={i} className="rounded-md border border-border bg-background p-4 space-y-3">
              {/* Title bar */}
              <div className="flex items-center gap-2 pb-2 border-b border-border">
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0 bg-card text-foreground border border-border font-semibold tabular-nums">
                  {i + 1}
                </Badge>
                <span className="text-sm font-bold text-foreground">
                  {String(copy.headline ?? `Ad copy ${i + 1}`)}
                </span>
              </div>

              {/* Two-column field grid */}
              <div className="grid grid-cols-2 gap-3">
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
      <Textarea
        placeholder="Optional feedback..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the ad copies. Continue to the next step in the WORKFLOW — generate visual concepts." })}>
          Approve &amp; continue
        </Button>
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

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
        <h2 className="text-lg font-semibold">Review visual concepts</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Edit the image prompt, aspect ratio, or style directly, and/or add a
        revision note (applied by the AI before rendering). Approve to generate
        images. Note: changing the style label alone won&apos;t change the image
        unless the prompt or note reflects it.
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
      <div className="flex gap-3">
        <Button onClick={submit}>Approve &amp; generate images</Button>
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
            className="text-[10px] px-1.5 py-0 font-semibold tabular-nums"
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
      <Textarea
        placeholder="Optional note for the agent (e.g. focus, angle)..."
        value={instruction}
        onChange={(e) => setInstruction(e.target.value)}
        rows={2}
      />
      <div className="flex gap-3">
        <Button
          disabled={selected.size === 0}
          onClick={() =>
            onResume({
              status: "selected",
              selected_trends: [...selected],
              instruction,
            })
          }
        >
          Confirm selection
        </Button>
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
  if (functionName === "review_research") {
    return <ReviewResearch state={sessionState} onResume={onResume} />;
  }
  if (functionName === "review_ad_copies") {
    return <ReviewAdCopies state={sessionState} onResume={onResume} />;
  }
  if (functionName === "review_visual_concepts") {
    return <ReviewVisualConcepts state={sessionState} onResume={onResume} />;
  }
  if (functionName === "review_trends") {
    return <ReviewTrends state={sessionState} onResume={onResume} />;
  }
  return null;
}
