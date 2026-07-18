"use client";

import { useState, useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
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
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
          <h2 className="text-lg font-semibold">Review Research Report</h2>
        </div>
        {report && (
          <button
            onClick={() => setEditMode((v) => !v)}
            className="text-xs font-medium text-primary hover:text-primary/80 transition-colors"
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
        <div className="max-h-[28rem] overflow-y-auto rounded-lg bg-muted/30 p-5 border border-border prose prose-sm prose-neutral max-w-none
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
          className="bg-background border-border font-mono text-xs leading-relaxed max-h-[28rem]"
        />
      )}
      <Textarea
        placeholder="Optional feedback or revision requests..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="bg-background border-border"
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the research. Continue to the next step in the WORKFLOW." })}>
          Approve &amp; Continue
        </Button>
        <Button
          variant="outline"
          onClick={() => onResume({ status: "revision_requested", feedback, instruction: "User requested changes to the research. Address their feedback, then continue the WORKFLOW." })}
          disabled={!feedback}
        >
          Request Changes
        </Button>
      </div>
    </div>
  );
}

/** Labeled field for review cards */
function ReviewField({ label, value, color }: { label: string; value: string; color: string }) {
  if (!value) return null;
  return (
    <div>
      <dt className={`text-[10px] font-bold uppercase tracking-wider ${color}`}>{label}</dt>
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
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
        <h2 className="text-lg font-semibold">Review Ad Copies</h2>
      </div>
      <p className="text-sm text-muted-foreground">
        Review the generated ad copies. Approve to continue to visual concept generation.
      </p>
      {adCopies && (
        <div className="space-y-3 max-h-[28rem] overflow-y-auto">
          {adCopies.map((copy, i) => (
            <div key={i} className="rounded-xl border border-border/60 bg-background/50 p-4 space-y-3">
              {/* Title bar */}
              <div className="flex items-center gap-2 pb-2 border-b border-border/40">
                <Badge variant="secondary" className="text-[10px] px-1.5 py-0 bg-indigo-100 text-indigo-600 border-0 font-bold">
                  {i + 1}
                </Badge>
                <span className="text-sm font-bold text-foreground">
                  {String(copy.headline ?? `Ad Copy ${i + 1}`)}
                </span>
              </div>

              {/* Two-column field grid */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Body Text" value={String(copy.body_text ?? "")} color="text-indigo-500" />
                </div>
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Tone / Style" value={String(copy.tone_style ?? "")} color="text-violet-500" />
                </div>
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Call to Action" value={String(copy.call_to_action ?? "")} color="text-amber-600" />
                </div>
                <div className="rounded-md bg-muted/40 px-3 py-2">
                  <ReviewField label="Trend Connection" value={String(copy.trend_connection ?? "")} color="text-emerald-600" />
                </div>
              </div>

              {/* Full-width fields */}
              {!!copy.audience_appeal_rationale && (
                <div className="rounded-md bg-pink-50/60 border border-pink-200/40 px-3 py-2">
                  <ReviewField label="Audience Appeal" value={String(copy.audience_appeal_rationale)} color="text-pink-600" />
                </div>
              )}
              {!!copy.social_caption && (
                <div className="rounded-md bg-amber-50/60 border border-amber-200/40 px-3 py-2">
                  <ReviewField label="Social Caption" value={String(copy.social_caption)} color="text-amber-500" />
                </div>
              )}
              {!!copy.detailed_performance_rationale && (
                <div className="rounded-md bg-orange-50/60 border border-orange-200/40 px-3 py-2">
                  <ReviewField label="Performance Rationale" value={String(copy.detailed_performance_rationale)} color="text-orange-500" />
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <Textarea
        placeholder="Optional feedback..."
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="bg-background border-border"
      />
      <div className="flex gap-3">
        <Button onClick={() => onResume({ status: "approved", feedback, instruction: "User approved the ad copies. Continue to the next step in the WORKFLOW — generate visual concepts." })}>
          Approve &amp; Continue
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
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center gap-2">
        <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
        <h2 className="text-lg font-semibold">Review Visual Concepts</h2>
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
            <div key={i} className="rounded-lg border p-4 space-y-3">
              <div className="font-medium">
                {String(concept.concept_name ?? `Concept ${i + 1}`)}
              </div>
              <div className="text-sm text-muted-foreground">
                {String(concept.concept_summary ?? "")}
              </div>

              <label className="block text-[10px] font-bold uppercase tracking-wider text-cyan-600">
                Image prompt
              </label>
              <Textarea
                value={drafts[i]?.image_generation_prompt ?? ""}
                onChange={(e) =>
                  update(i, "image_generation_prompt", e.target.value)
                }
                rows={4}
                className="bg-muted/30 border-border font-mono text-xs leading-relaxed"
              />

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] font-bold uppercase tracking-wider text-violet-500">
                    Aspect ratio
                  </label>
                  <select
                    value={drafts[i]?.aspect_ratio ?? ""}
                    onChange={(e) => update(i, "aspect_ratio", e.target.value)}
                    className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm"
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
                  <label className="block text-[10px] font-bold uppercase tracking-wider text-amber-600">
                    Style
                  </label>
                  <Input
                    value={drafts[i]?.visual_style ?? ""}
                    onChange={(e) => update(i, "visual_style", e.target.value)}
                    className="mt-1 bg-background border-border text-sm"
                  />
                </div>
              </div>

              <label className="block text-[10px] font-bold uppercase tracking-wider text-emerald-600">
                Revision note (applied by AI)
              </label>
              <Input
                placeholder="e.g., make the background brighter, add a dog"
                value={drafts[i]?.revision_note ?? ""}
                onChange={(e) => update(i, "revision_note", e.target.value)}
                className="bg-background border-border text-sm"
              />
            </div>
          ))}
        </div>
      )}
      <div className="flex gap-3">
        <Button onClick={submit}>Approve &amp; Generate Images</Button>
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
    <div className="glass rounded-2xl p-6 space-y-4 animate-fadeInUp">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-amber-500" />
          <h2 className="text-lg font-semibold">Pick Your Trends</h2>
        </div>
        {candidates.length > 0 && (
          <Badge
            variant="secondary"
            className="text-[10px] px-1.5 py-0 bg-primary/10 text-primary border-0 font-bold"
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
                className={`glass rounded-xl px-4 py-3 text-left text-sm transition-all duration-200 hover:shadow-md hover:shadow-black/5 ${
                  isSelected
                    ? "ring-2 ring-primary bg-primary/10 text-primary font-semibold"
                    : "text-foreground/85"
                }`}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={`inline-flex h-4 w-4 shrink-0 items-center justify-center rounded border text-[10px] font-bold ${
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
        className="bg-background border-border"
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
          Confirm Selection
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
