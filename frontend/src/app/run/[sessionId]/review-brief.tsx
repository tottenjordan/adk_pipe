"use client";

import React, { useId, useState } from "react";
import { PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import {
  FIT_MODE_OPTIONS,
  MAX_ANGLES,
  MIN_ANGLES,
  buildBriefEdit,
  emptyAngle,
  validateBriefDraft,
  type BriefErrors,
} from "@/lib/brief-edit";
import type { CreativeBrief, FitMode } from "@/lib/creative-brief";
import { cn } from "@/lib/utils";
import { ResearchReportEditor, useReportEdit } from "./report-editor";
import { useReviewDraft } from "./review-drafts";
import { ACTIONS_ROW, ShortcutHint, useApproveShortcut } from "./review-shared";

const SECTION = "space-y-3 border-t border-border pt-4";
const SELECT =
  "mt-1 w-full rounded-sm border border-input bg-card px-2 py-1.5 text-sm aria-invalid:border-destructive";

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="mt-1 text-xs text-destructive">
      {message}
    </p>
  );
}

/** Props wiring a control to its label and (optional) error message. */
function a11y(id: string, error?: string) {
  return {
    id,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? `${id}-error` : undefined,
  } as const;
}

/** A text field (single line or multi-line) with label and error. */
function TextField({
  id,
  label,
  value,
  onChange,
  error,
  rows,
  className,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  rows?: number;
  className?: string;
}) {
  return (
    <div className={className}>
      <FieldLabel as="label" htmlFor={id}>
        {label}
      </FieldLabel>
      {rows ? (
        <Textarea
          {...a11y(id, error)}
          value={value}
          rows={rows}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 text-sm leading-relaxed"
        />
      ) : (
        <Input
          {...a11y(id, error)}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 text-sm"
        />
      )}
      <FieldError id={`${id}-error`} message={error} />
    </div>
  );
}

function RemoveButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <Button type="button" variant="ghost" size="icon-sm" aria-label={label} onClick={onClick} disabled={disabled}>
      <XIcon aria-hidden="true" />
    </Button>
  );
}

function AddButton({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <Button type="button" variant="outline" size="sm" onClick={onClick} disabled={disabled}>
      <PlusIcon aria-hidden="true" />
      {children}
    </Button>
  );
}

/** An editable list of short strings (add / remove rows). */
function StringListEditor({
  id,
  label,
  itemLabel,
  items,
  onChange,
}: {
  id: string;
  label: string;
  /** Singular noun for row labels and the add button, e.g. "mandatory". */
  itemLabel: string;
  items: string[];
  onChange: (items: string[]) => void;
}) {
  return (
    <fieldset className="space-y-2">
      <FieldLabel as="legend">{label}</FieldLabel>
      {items.map((item, i) => (
        <div key={i} className="flex items-center gap-2">
          <Input
            aria-label={`${label}, ${itemLabel} ${i + 1}`}
            id={`${id}-${i}`}
            value={item}
            onChange={(e) => onChange(items.map((v, j) => (j === i ? e.target.value : v)))}
            className="text-sm"
          />
          <RemoveButton
            label={`Remove ${itemLabel} ${i + 1}`}
            onClick={() => onChange(items.filter((_, j) => j !== i))}
          />
        </div>
      ))}
      <AddButton onClick={() => onChange([...items, ""])}>Add {itemLabel}</AddButton>
    </fieldset>
  );
}

/**
 * The structured creative brief as an editable form (checkpoint 1). Controlled:
 * the parent owns the draft and the validation errors.
 */
export function BriefEditForm({
  draft,
  onChange,
  errors,
}: {
  draft: CreativeBrief;
  onChange: (draft: CreativeBrief) => void;
  errors: BriefErrors;
}) {
  const uid = useId();
  const id = (name: string) => `${uid}-${name}`;
  const set = <K extends keyof CreativeBrief>(key: K, value: CreativeBrief[K]) =>
    onChange({ ...draft, [key]: value });
  const tb = draft.trendBridge;
  const setBridge = (patch: Partial<CreativeBrief["trendBridge"]>) => set("trendBridge", { ...tb, ...patch });
  const angles = draft.angles;
  const setAngle = (i: number, patch: Partial<CreativeBrief["angles"][number]>) =>
    set(
      "angles",
      angles.map((a, j) => (j === i ? { ...a, ...patch } : a))
    );
  const rtbs = draft.reasonsToBelieve;
  const setRtb = (i: number, patch: Partial<CreativeBrief["reasonsToBelieve"][number]>) =>
    set(
      "reasonsToBelieve",
      rtbs.map((r, j) => (j === i ? { ...r, ...patch } : r))
    );

  return (
    <div className="space-y-4">
      <TextField
        id={id("smp")}
        label="Single-minded proposition"
        value={draft.singleMindedProposition}
        onChange={(v) => set("singleMindedProposition", v)}
        error={errors.single_minded_proposition}
        rows={2}
      />
      <TextField id={id("insight")} label="Insight" value={draft.insight} onChange={(v) => set("insight", v)} error={errors.insight} rows={2} />
      <div className="grid gap-3 sm:grid-cols-3">
        <TextField id={id("objective")} label="Objective" value={draft.objective} onChange={(v) => set("objective", v)} error={errors.objective} rows={3} />
        <TextField id={id("audience")} label="Audience" value={draft.audience} onChange={(v) => set("audience", v)} error={errors.audience} rows={3} />
        <TextField
          id={id("response")}
          label="Desired response"
          value={draft.desiredResponse}
          onChange={(v) => set("desiredResponse", v)}
          error={errors.desired_response}
          rows={3}
        />
      </div>

      <div className={SECTION}>
        <div className="grid gap-3 sm:grid-cols-[8rem_12rem_minmax(0,1fr)]">
          <div>
            <FieldLabel as="label" htmlFor={id("fit-score")}>
              Trend fit (1–5)
            </FieldLabel>
            <select
              {...a11y(id("fit-score"), errors["trend_bridge.fit_score"])}
              value={tb.fitScore ?? ""}
              onChange={(e) => setBridge({ fitScore: e.target.value ? Number(e.target.value) : null })}
              className={SELECT}
            >
              {tb.fitScore === null && <option value="">Choose</option>}
              {[1, 2, 3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            <FieldError id={`${id("fit-score")}-error`} message={errors["trend_bridge.fit_score"]} />
          </div>
          <div>
            <FieldLabel as="label" htmlFor={id("fit-mode")}>
              Fit mode
            </FieldLabel>
            <select
              {...a11y(id("fit-mode"), errors["trend_bridge.fit_mode"])}
              value={tb.fitMode ?? ""}
              onChange={(e) => setBridge({ fitMode: (e.target.value || null) as FitMode | null })}
              className={SELECT}
            >
              {tb.fitMode === null && <option value="">Choose</option>}
              {FIT_MODE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <FieldError id={`${id("fit-mode")}-error`} message={errors["trend_bridge.fit_mode"]} />
          </div>
          <TextField id={id("bridge")} label="Trend bridge" value={tb.bridge} onChange={(v) => setBridge({ bridge: v })} error={errors["trend_bridge.bridge"]} />
        </div>
        <TextField
          id={id("tone")}
          label="Brand tone of voice"
          value={draft.brand.toneOfVoice}
          onChange={(v) => set("brand", { ...draft.brand, toneOfVoice: v })}
          error={errors["brand.tone_of_voice"]}
        />
      </div>

      <fieldset className={SECTION}>
        <FieldLabel as="legend">Reasons to believe</FieldLabel>
        {rtbs.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <Input
              aria-label={`Reason ${i + 1} claim`}
              value={r.claim}
              onChange={(e) => setRtb(i, { claim: e.target.value })}
              className="text-sm"
            />
            <Input
              aria-label={`Reason ${i + 1} source id`}
              placeholder="src-1 or brief"
              value={r.sourceId ?? ""}
              onChange={(e) => setRtb(i, { sourceId: e.target.value || null })}
              className="w-32 shrink-0 font-mono text-xs"
            />
            <RemoveButton
              label={`Remove reason ${i + 1}`}
              onClick={() =>
                set(
                  "reasonsToBelieve",
                  rtbs.filter((_, j) => j !== i)
                )
              }
            />
          </div>
        ))}
        <AddButton onClick={() => set("reasonsToBelieve", [...rtbs, { claim: "", sourceId: null }])}>
          Add reason
        </AddButton>
      </fieldset>

      <fieldset className={SECTION} aria-describedby={errors.angles ? `${id("angles")}-error` : undefined}>
        <FieldLabel as="legend">
          Creative angles ({MIN_ANGLES}–{MAX_ANGLES})
        </FieldLabel>
        {angles.map((a, i) => (
          <div key={a.angleId || i} className="space-y-2 rounded-md border border-border bg-background p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-xs text-muted-foreground">{a.angleId}</span>
              <FieldError id={id(`angle-${i}-id-error`)} message={errors[`angles.${i}.angle_id`]} />
              <RemoveButton
                label={`Remove angle ${a.angleId || i + 1}`}
                onClick={() =>
                  set(
                    "angles",
                    angles.filter((_, j) => j !== i)
                  )
                }
                disabled={angles.length <= MIN_ANGLES}
              />
            </div>
            <TextField
              id={id(`angle-${i}-name`)}
              label={`Angle ${a.angleId || i + 1} name`}
              value={a.name}
              onChange={(v) => setAngle(i, { name: v })}
              error={errors[`angles.${i}.name`]}
            />
            <TextField id={id(`angle-${i}-tension`)} label="Tension" value={a.tension} onChange={(v) => setAngle(i, { tension: v })} error={errors[`angles.${i}.tension`]} />
            <TextField id={id(`angle-${i}-route`)} label="Route" value={a.route} onChange={(v) => setAngle(i, { route: v })} error={errors[`angles.${i}.route`]} rows={2} />
          </div>
        ))}
        <FieldError id={`${id("angles")}-error`} message={errors.angles} />
        <AddButton onClick={() => set("angles", [...angles, emptyAngle(angles)])} disabled={angles.length >= MAX_ANGLES}>
          Add angle
        </AddButton>
      </fieldset>

      <div className={cn(SECTION, "grid gap-4 sm:grid-cols-2 sm:space-y-0")}>
        <StringListEditor
          id={id("mandatories")}
          label="Mandatories"
          itemLabel="mandatory"
          items={draft.mandatories}
          onChange={(v) => set("mandatories", v)}
        />
        <StringListEditor id={id("avoid")} label="Avoid" itemLabel="avoid item" items={draft.avoid} onChange={(v) => set("avoid", v)} />
      </div>
    </div>
  );
}

/**
 * Checkpoint 1 when the run has a structured brief: the editable brief first,
 * the full research report collapsed underneath (still editable). The resume
 * carries `edits` for whichever changed plus `brief_edited` / `report_edited`.
 */
export function ReviewBrief({
  brief,
  state,
  onResume,
  draftKey,
  serverErrors = {},
}: {
  brief: CreativeBrief;
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
  /** The checkpoint call id: keeps the drafts across a rejected resume. */
  draftKey?: string;
  /** Field errors from a rejected (400) resume, shown until the brief changes. */
  serverErrors?: BriefErrors;
}) {
  const key = (part: string) => (draftKey ? `${draftKey}:${part}` : undefined);
  const [feedback, setFeedback] = useReviewDraft(key("feedback"), () => "");
  const [draft, setDraftValue] = useReviewDraft<CreativeBrief>(key("brief"), () => structuredClone(brief));
  // Server errors describe the brief as submitted; hide them once it changes
  // (the panel remounts after each rejected resume, resetting this).
  const [staleServerErrors, setStaleServerErrors] = useState(false);
  const setDraft = (next: CreativeBrief) => {
    setDraftValue(next);
    setStaleServerErrors(true);
  };
  const reportEdit = useReportEdit(state, draftKey);
  const briefEdit = buildBriefEdit(brief, draft);
  // Only an edited brief is validated: an untouched one is sent as no edit,
  // so even a brief the writer left imperfect can always be approved as is.
  const errors = briefEdit ? validateBriefDraft(draft) : {};
  const errorCount = Object.keys(errors).length;
  const valid = errorCount === 0;
  const shownErrors = { ...(staleServerErrors ? {} : serverErrors), ...errors };
  const reportEdits = reportEdit.edits;
  const edits = [...(briefEdit ?? []), ...(reportEdits ?? [])];

  const resume = (status: string, instruction: string) => {
    if (!valid) return;
    onResume({
      status,
      feedback,
      instruction,
      ...(edits.length ? { edits } : {}),
      ...(briefEdit ? { brief_edited: true } : {}),
      ...(reportEdits ? { report_edited: true } : {}),
    });
  };
  const approve = () =>
    resume("approved", "User approved the creative brief. Continue to the next step in the WORKFLOW.");
  useApproveShortcut(approve, valid);

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
          <h2 className="text-lg font-semibold">Review creative brief</h2>
        </div>
        {briefEdit && <span className="text-xs text-muted-foreground">Edited</span>}
      </div>
      <p className="text-sm text-muted-foreground">
        The ad copy and visuals are written to this brief. Edit any field to
        change them; the research PDF is regenerated. Feedback is passed on as
        guidance.
      </p>

      <BriefEditForm draft={draft} onChange={setDraft} errors={shownErrors} />

      <details className="group rounded-md border border-border bg-background">
        <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium text-foreground">
          Full research report
          {reportEdits && <span className="ml-2 text-xs font-normal text-muted-foreground">Edited</span>}
        </summary>
        <div className="border-t border-border p-4">
          <ResearchReportEditor edit={reportEdit} idPrefix="review-brief-report" />
        </div>
      </details>

      <FieldLabel as="label" htmlFor="review-brief-feedback">
        Feedback (optional)
      </FieldLabel>
      <Textarea
        id="review-brief-feedback"
        placeholder="e.g. lean on the nostalgia angle, skip the price comparison"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={3}
        className="-mt-2"
      />
      {!valid && (
        <p role="status" className="text-sm text-destructive">
          Fix {errorCount === 1 ? "1 field" : `${errorCount} fields`} in the brief to continue.
        </p>
      )}
      <div className={ACTIONS_ROW}>
        <Button onClick={approve} disabled={!valid}>
          Approve &amp; continue
        </Button>
        <Button
          variant="outline"
          onClick={() =>
            resume(
              "revision_requested",
              "User requested changes to the creative brief. Address their feedback, then continue the WORKFLOW."
            )
          }
          disabled={!valid || (!feedback && edits.length === 0)}
        >
          Request changes
        </Button>
        <ShortcutHint />
      </div>
    </div>
  );
}
