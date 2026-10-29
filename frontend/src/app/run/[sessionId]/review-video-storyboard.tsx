"use client";

import React, { useId, useMemo, useState } from "react";
import { ArrowDownIcon, ArrowUpIcon, PlusIcon, XIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FieldLabel } from "@/components/field-label";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import {
  BUMPER_DURATION_SECONDS,
  HERO_DURATION_SECONDS,
  MAX_STORYBOARD_SHOTS,
  MIN_STORYBOARD_SHOTS,
  SHOT_DURATION_OPTIONS,
  SOCIAL_MAX_SECONDS,
  SOCIAL_MIN_SECONDS,
  buildStoryboardEdit,
  computeCutTotals,
  countWords,
  createEmptyShot,
  extractApprovedSupers,
  extractConceptOptions,
  fallbackVideoStoryboard,
  maxVoWordsForDuration,
  parseIssueList,
  parseVideoStoryboard,
  validateStoryboardDraft,
  type ShotDurationSeconds,
  type StoryboardErrors,
  type VideoShotSpec,
  type VideoStoryboard,
} from "@/lib/video-storyboard";
import { useReviewDraft } from "./review-drafts";
import { ACTIONS_ROW, ShortcutHint, useApproveShortcut } from "./review-shared";

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

function a11y(id: string, error?: string) {
  return {
    id,
    "aria-invalid": error ? true : undefined,
    "aria-describedby": error ? `${id}-error` : undefined,
  } as const;
}

function renumberShots(shots: VideoShotSpec[]): VideoShotSpec[] {
  return shots.map((shot, idx) => ({
    ...shot,
    shot_number: idx + 1,
  }));
}

/**
 * Interactive Checkpoint 4 (`review_video_storyboard`): editable commercial
 * storyboard review before rendering the 30s hero, 15s social, and 6s bumper cuts.
 */
export function ReviewVideoStoryboard({
  state,
  onResume,
  draftKey,
  serverErrors = {},
}: {
  state: Record<string, unknown>;
  onResume: (response: Record<string, unknown>) => void;
  /** Checkpoint function call id keeping drafts intact across a 400 rejection. */
  draftKey?: string;
  /** Server-side validation errors from `StoryboardEditError` (HTTP 400). */
  serverErrors?: StoryboardErrors;
}) {
  const uid = useId();
  const fieldId = (name: string) => `${uid}-${name}`;

  const initialStoryboard = useMemo(
    () => parseVideoStoryboard(state.video_storyboard),
    [state.video_storyboard]
  );
  const seedStoryboard = useMemo(
    () => initialStoryboard ?? fallbackVideoStoryboard(state),
    [initialStoryboard, state]
  );

  // Prefix with `${draftKey}:` so `clearDraftsFor(functionCallId)` evicts the
  // draft once a resume is accepted while keeping edits across 400/409 remounts.
  const key = (part: string) =>
    draftKey ? `${draftKey}:storyboard-${part}` : undefined;
  const [draft, setDraftValue] = useReviewDraft<VideoStoryboard>(
    key("draft"),
    () => structuredClone(seedStoryboard)
  );
  const [feedback, setFeedback] = useReviewDraft(key("feedback"), () => "");
  const [dismissedServerErrors, setDismissedServerErrors] =
    useState<StoryboardErrors | null>(null);
  const staleServerErrors = dismissedServerErrors === serverErrors;

  const setDraft = (next: VideoStoryboard) => {
    setDraftValue(next);
    setDismissedServerErrors(serverErrors);
  };

  const concepts = useMemo(() => extractConceptOptions(state), [state]);
  const conceptThumbByName = useMemo(() => {
    const map = new Map<string, string>();
    for (const c of concepts) {
      const parsed = parseGsUri(c.gcsUri);
      if (parsed) {
        map.set(c.name.toLowerCase(), gcsProxyUrl(parsed.bucket, parsed.path));
      }
    }
    return map;
  }, [concepts]);

  const approvedSupers = useMemo(() => extractApprovedSupers(state), [state]);
  const existingGateIssues = useMemo(
    () => parseIssueList(state.video_storyboard__issues),
    [state.video_storyboard__issues]
  );

  const storyboardEdits = buildStoryboardEdit(initialStoryboard, draft);
  const clientErrors = validateStoryboardDraft(draft, state);
  const activeServerErrors = staleServerErrors ? {} : serverErrors;
  const shownErrors: StoryboardErrors = {
    ...activeServerErrors,
    ...clientErrors,
  };

  const clientErrorCount = Object.keys(clientErrors).length;
  const serverErrorCount = Object.keys(activeServerErrors).length;
  const valid = clientErrorCount === 0 && serverErrorCount === 0;
  const totals = computeCutTotals(draft.shots);

  const bannerServerMessages = [
    ...(activeServerErrors[""] ? activeServerErrors[""].split("\n") : []),
    ...(activeServerErrors.shots ? activeServerErrors.shots.split("\n") : []),
  ]
    .map((m) => m.trim())
    .filter(Boolean);

  const setField = <K extends keyof VideoStoryboard>(
    k: K,
    val: VideoStoryboard[K]
  ) => setDraft({ ...draft, [k]: val });

  const updateShot = (index: number, patch: Partial<VideoShotSpec>) => {
    setDraft({
      ...draft,
      shots: draft.shots.map((shot, i) =>
        i === index ? { ...shot, ...patch } : shot
      ),
    });
  };

  const moveShot = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= draft.shots.length) return;
    const copy = [...draft.shots];
    const [moved] = copy.splice(index, 1);
    copy.splice(target, 0, moved);
    setDraft({ ...draft, shots: renumberShots(copy) });
  };

  const removeShot = (index: number) => {
    if (draft.shots.length <= MIN_STORYBOARD_SHOTS) return;
    setDraft({
      ...draft,
      shots: renumberShots(draft.shots.filter((_, i) => i !== index)),
    });
  };

  const addShot = () => {
    if (draft.shots.length >= MAX_STORYBOARD_SHOTS) return;
    setDraft({
      ...draft,
      shots: renumberShots([...draft.shots, createEmptyShot(draft.shots, state)]),
    });
  };

  const approve = () => {
    if (!valid) return;
    onResume({
      status: "approved",
      feedback,
      instruction:
        "User approved the video storyboard. Continue to the next step in the WORKFLOW — call video_render_pipeline to render the multi-cut video reel, then proceed to finalize_pipeline.",
      ...(storyboardEdits
        ? { edits: storyboardEdits, storyboard_edited: true }
        : {}),
    });
  };

  useApproveShortcut(approve, valid);

  const supersDatalistId = fieldId("approved-supers");

  return (
    <div className="rounded-lg border border-border bg-card p-6 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-mark-pending" />
          <h2 className="text-lg font-semibold">Review video storyboard</h2>
        </div>
        {storyboardEdits && (
          <span className="text-xs text-muted-foreground">Edited</span>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        Inspect and edit each shot before rendering the 30-second 16:9 hero spot
        and the 15-second and 6-second 9:16 social cutdowns. Every shot is
        anchored to a finalized Phase 2 keyframe.
      </p>

      {/* Live cut-duration summary bar */}
      <div
        aria-label="Cut duration totals"
        className="grid gap-2.5 sm:grid-cols-3"
      >
        <div
          className={`rounded-md border px-3 py-2 text-xs tabular-nums ${
            totals.heroValid
              ? "border-mark-pass/40 bg-mark-pass/10 text-mark-pass"
              : "border-destructive/40 bg-destructive/10 text-destructive"
          }`}
        >
          <div className="font-semibold">
            30s Hero: {totals.heroSeconds}s / {HERO_DURATION_SECONDS}s
          </div>
          <div className="text-[11px] opacity-85">
            {draft.shots.length} shot{draft.shots.length === 1 ? "" : "s"} (16:9
            master)
          </div>
        </div>

        <div
          className={`rounded-md border px-3 py-2 text-xs tabular-nums ${
            totals.socialValid
              ? "border-mark-pass/40 bg-mark-pass/10 text-mark-pass"
              : "border-destructive/40 bg-destructive/10 text-destructive"
          }`}
        >
          <div className="font-semibold">
            15s Social: {totals.socialSeconds}s ({totals.socialShotCount} shot
            {totals.socialShotCount === 1 ? "" : "s"})
          </div>
          <div className="text-[11px] opacity-85">
            Target {SOCIAL_MIN_SECONDS}–{SOCIAL_MAX_SECONDS}s across ≥2 shots
            (9:16)
          </div>
        </div>

        <div
          className={`rounded-md border px-3 py-2 text-xs tabular-nums ${
            totals.bumperValid
              ? "border-mark-pass/40 bg-mark-pass/10 text-mark-pass"
              : "border-destructive/40 bg-destructive/10 text-destructive"
          }`}
        >
          <div className="font-semibold">
            6s Bumper: {totals.bumperSeconds}s ({totals.bumperShotCount} shot
            {totals.bumperShotCount === 1 ? "" : "s"})
          </div>
          <div className="text-[11px] opacity-85">
            Target {BUMPER_DURATION_SECONDS}s across ≥1 shot (9:16)
          </div>
        </div>
      </div>

      {/* Cut/list-level validation messages */}
      {(clientErrors.shots ||
        clientErrors.hero_duration ||
        clientErrors.social_duration ||
        clientErrors.bumper_duration) && (
        <ul
          aria-label="Storyboard timing issues"
          className="list-disc space-y-1 rounded-md border border-destructive/40 bg-destructive/10 py-2.5 pr-3 pl-7 text-xs text-destructive"
        >
          {clientErrors.shots && <li>{clientErrors.shots}</li>}
          {clientErrors.hero_duration && <li>{clientErrors.hero_duration}</li>}
          {clientErrors.social_duration && (
            <li>{clientErrors.social_duration}</li>
          )}
          {clientErrors.bumper_duration && (
            <li>{clientErrors.bumper_duration}</li>
          )}
        </ul>
      )}

      {/* Server-side 400 validation errors */}
      {bannerServerMessages.length > 0 && (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-xs text-destructive space-y-1"
        >
          <p className="font-semibold">Storyboard validation failed</p>
          <ul className="list-disc space-y-0.5 pl-4">
            {bannerServerMessages.map((msg, idx) => (
              <li key={idx}>{msg}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Pre-existing automated gate notes */}
      {!storyboardEdits && existingGateIssues.length > 0 && (
        <div className="rounded-md border border-mark-pending/40 bg-mark-pending/10 px-4 py-3 text-xs text-foreground space-y-1">
          <p className="font-semibold text-mark-pending">
            Automated storyboard gate notes
          </p>
          <ul className="list-disc space-y-0.5 pl-4 text-muted-foreground">
            {existingGateIssues.map((issue, idx) => (
              <li key={idx}>{issue}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Top-level storyboard fields */}
      <div className="space-y-3 border-t border-border pt-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <FieldLabel as="label" htmlFor={fieldId("title")}>
              Spot title
            </FieldLabel>
            <Input
              {...a11y(fieldId("title"), shownErrors.title)}
              value={draft.title}
              onChange={(e) => setField("title", e.target.value)}
              className="mt-1 text-sm"
            />
            <FieldError
              id={`${fieldId("title")}-error`}
              message={shownErrors.title}
            />
          </div>

          <div>
            <FieldLabel as="label" htmlFor={fieldId("voiceover-style")}>
              Voiceover style
            </FieldLabel>
            <Input
              {...a11y(fieldId("voiceover-style"), shownErrors.voiceover_style)}
              value={draft.voiceover_style}
              onChange={(e) => setField("voiceover_style", e.target.value)}
              placeholder="e.g. Warm, confident commercial delivery"
              className="mt-1 text-sm"
            />
            <FieldError
              id={`${fieldId("voiceover-style")}-error`}
              message={shownErrors.voiceover_style}
            />
          </div>
        </div>

        <div>
          <FieldLabel as="label" htmlFor={fieldId("creative-direction")}>
            Creative direction
          </FieldLabel>
          <Textarea
            {...a11y(
              fieldId("creative-direction"),
              shownErrors.creative_direction
            )}
            value={draft.creative_direction}
            onChange={(e) => setField("creative_direction", e.target.value)}
            rows={2}
            className="mt-1 text-sm leading-relaxed"
          />
          <FieldError
            id={`${fieldId("creative-direction")}-error`}
            message={shownErrors.creative_direction}
          />
        </div>

        <div>
          <FieldLabel as="label" htmlFor={fieldId("music-direction")}>
            Instrumental music direction
          </FieldLabel>
          <Input
            {...a11y(fieldId("music-direction"), shownErrors.music_direction)}
            value={draft.music_direction}
            onChange={(e) => setField("music_direction", e.target.value)}
            placeholder="Strictly instrumental music bed (no vocals)"
            className="mt-1 text-sm"
          />
          <FieldError
            id={`${fieldId("music-direction")}-error`}
            message={shownErrors.music_direction}
          />
        </div>
      </div>

      {/* Shot cards */}
      <fieldset className="space-y-3 border-t border-border pt-4">
        <FieldLabel as="legend">
          Storyboard shots ({MIN_STORYBOARD_SHOTS}–{MAX_STORYBOARD_SHOTS})
        </FieldLabel>

        {approvedSupers.length > 0 && (
          <datalist id={supersDatalistId}>
            {approvedSupers.map((sup) => (
              <option key={sup} value={sup} />
            ))}
          </datalist>
        )}

        <div className="space-y-3">
          {draft.shots.map((shot, i) => {
            const shotNum = i + 1;
            const voWords = countWords(shot.voiceover_line);
            const maxVoWords = maxVoWordsForDuration(shot.duration_seconds);
            const voOverLimit = voWords > maxVoWords;
            const thumbUrl = conceptThumbByName.get(
              shot.anchor_concept_name.trim().toLowerCase()
            );

            const durErr = shownErrors[`shots.${i}.duration_seconds`];
            const anchorErr = shownErrors[`shots.${i}.anchor_concept_name`];
            const cameraErr = shownErrors[`shots.${i}.camera_motion`];
            const actionErr = shownErrors[`shots.${i}.visual_action_prompt`];
            const voErr = shownErrors[`shots.${i}.voiceover_line`];
            const superErr = shownErrors[`shots.${i}.on_screen_super`];
            const audioErr = shownErrors[`shots.${i}.audio_cue`];

            return (
              <div
                key={i}
                className="rounded-md border border-border bg-background p-4 space-y-3"
              >
                {/* Shot card header */}
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2.5">
                  <div className="flex items-center gap-2.5">
                    <Badge
                      variant="secondary"
                      className="border border-border bg-card px-2 py-0.5 text-xs font-semibold text-foreground tabular-nums"
                    >
                      Shot {shotNum}
                    </Badge>
                    {thumbUrl && (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={thumbUrl}
                        alt=""
                        className="h-8 w-8 rounded-xs border border-border object-cover"
                      />
                    )}
                    <span className="text-xs text-muted-foreground tabular-nums">
                      {shot.duration_seconds}s
                      {shot.anchor_concept_name
                        ? ` · ${shot.anchor_concept_name}`
                        : ""}
                    </span>
                  </div>

                  <div className="flex items-center gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Move shot ${shotNum} up`}
                      disabled={i === 0}
                      onClick={() => moveShot(i, -1)}
                    >
                      <ArrowUpIcon aria-hidden="true" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Move shot ${shotNum} down`}
                      disabled={i === draft.shots.length - 1}
                      onClick={() => moveShot(i, 1)}
                    >
                      <ArrowDownIcon aria-hidden="true" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Remove shot ${shotNum}`}
                      disabled={draft.shots.length <= MIN_STORYBOARD_SHOTS}
                      onClick={() => removeShot(i)}
                    >
                      <XIcon aria-hidden="true" />
                    </Button>
                  </div>
                </div>

                {/* Duration, Anchor concept & Cutdown checkboxes */}
                <div className="grid gap-3 sm:grid-cols-[7rem_minmax(0,1fr)_auto]">
                  <div>
                    <FieldLabel
                      as="label"
                      htmlFor={fieldId(`shot-${i}-duration`)}
                    >
                      Shot {shotNum} duration
                    </FieldLabel>
                    <select
                      {...a11y(fieldId(`shot-${i}-duration`), durErr)}
                      value={shot.duration_seconds}
                      onChange={(e) =>
                        updateShot(i, {
                          duration_seconds: Number(
                            e.target.value
                          ) as ShotDurationSeconds,
                        })
                      }
                      className={SELECT}
                    >
                      {SHOT_DURATION_OPTIONS.map((d) => (
                        <option key={d} value={d}>
                          {d}s
                        </option>
                      ))}
                    </select>
                    <FieldError
                      id={`${fieldId(`shot-${i}-duration`)}-error`}
                      message={durErr}
                    />
                  </div>

                  <div>
                    <FieldLabel
                      as="label"
                      htmlFor={fieldId(`shot-${i}-anchor`)}
                    >
                      Shot {shotNum} anchor concept
                    </FieldLabel>
                    {concepts.length > 0 ? (
                      <select
                        {...a11y(fieldId(`shot-${i}-anchor`), anchorErr)}
                        value={shot.anchor_concept_name}
                        onChange={(e) =>
                          updateShot(i, { anchor_concept_name: e.target.value })
                        }
                        className={SELECT}
                      >
                        {!shot.anchor_concept_name && (
                          <option value="">Choose concept</option>
                        )}
                        {shot.anchor_concept_name &&
                          !concepts.some(
                            (c) =>
                              c.name.toLowerCase() ===
                              shot.anchor_concept_name.trim().toLowerCase()
                          ) && (
                            <option value={shot.anchor_concept_name}>
                              {shot.anchor_concept_name}
                            </option>
                          )}
                        {concepts.map((c) => (
                          <option key={c.name} value={c.name}>
                            {c.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <Input
                        {...a11y(fieldId(`shot-${i}-anchor`), anchorErr)}
                        value={shot.anchor_concept_name}
                        onChange={(e) =>
                          updateShot(i, { anchor_concept_name: e.target.value })
                        }
                        className="mt-1 text-sm"
                      />
                    )}
                    <FieldError
                      id={`${fieldId(`shot-${i}-anchor`)}-error`}
                      message={anchorErr}
                    />
                  </div>

                  <div className="flex flex-wrap items-end gap-3 pb-1">
                    <label
                      htmlFor={fieldId(`shot-${i}-15s`)}
                      className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-foreground select-none"
                    >
                      <input
                        id={fieldId(`shot-${i}-15s`)}
                        type="checkbox"
                        checked={shot.include_in_15s}
                        onChange={(e) =>
                          updateShot(i, { include_in_15s: e.target.checked })
                        }
                        className="h-4 w-4 rounded border-border accent-primary"
                      />
                      15s Social
                    </label>
                    <label
                      htmlFor={fieldId(`shot-${i}-6s`)}
                      className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-foreground select-none"
                    >
                      <input
                        id={fieldId(`shot-${i}-6s`)}
                        type="checkbox"
                        checked={shot.include_in_6s}
                        onChange={(e) =>
                          updateShot(i, { include_in_6s: e.target.checked })
                        }
                        className="h-4 w-4 rounded border-border accent-primary"
                      />
                      6s Bumper
                    </label>
                  </div>
                </div>

                {/* Camera motion */}
                <div>
                  <FieldLabel as="label" htmlFor={fieldId(`shot-${i}-camera`)}>
                    Shot {shotNum} camera motion
                  </FieldLabel>
                  <Input
                    {...a11y(fieldId(`shot-${i}-camera`), cameraErr)}
                    value={shot.camera_motion}
                    onChange={(e) =>
                      updateShot(i, { camera_motion: e.target.value })
                    }
                    className="mt-1 text-sm"
                  />
                  <FieldError
                    id={`${fieldId(`shot-${i}-camera`)}-error`}
                    message={cameraErr}
                  />
                </div>

                {/* Visual action prompt */}
                <div>
                  <FieldLabel as="label" htmlFor={fieldId(`shot-${i}-action`)}>
                    Shot {shotNum} visual action
                  </FieldLabel>
                  <Textarea
                    {...a11y(fieldId(`shot-${i}-action`), actionErr)}
                    value={shot.visual_action_prompt}
                    onChange={(e) =>
                      updateShot(i, { visual_action_prompt: e.target.value })
                    }
                    rows={2}
                    className="mt-1 text-sm leading-relaxed"
                  />
                  <FieldError
                    id={`${fieldId(`shot-${i}-action`)}-error`}
                    message={actionErr}
                  />
                </div>

                {/* Voiceover line with live word-rate counter */}
                <div>
                  <div className="flex items-center justify-between gap-2">
                    <FieldLabel as="label" htmlFor={fieldId(`shot-${i}-vo`)}>
                      Shot {shotNum} voiceover line
                    </FieldLabel>
                    <span
                      className={`text-xs tabular-nums ${
                        voOverLimit
                          ? "font-semibold text-destructive"
                          : "text-muted-foreground"
                      }`}
                    >
                      {voWords}/{maxVoWords} words
                    </span>
                  </div>
                  <Input
                    {...a11y(fieldId(`shot-${i}-vo`), voErr)}
                    value={shot.voiceover_line}
                    onChange={(e) =>
                      updateShot(i, { voiceover_line: e.target.value })
                    }
                    placeholder="Leave blank for instrumental / silent beat"
                    className="mt-1 text-sm"
                  />
                  <FieldError
                    id={`${fieldId(`shot-${i}-vo`)}-error`}
                    message={voErr}
                  />
                </div>

                {/* On-screen super & Audio cue */}
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <FieldLabel as="label" htmlFor={fieldId(`shot-${i}-super`)}>
                      Shot {shotNum} on-screen super
                    </FieldLabel>
                    <Input
                      {...a11y(fieldId(`shot-${i}-super`), superErr)}
                      list={
                        approvedSupers.length > 0 ? supersDatalistId : undefined
                      }
                      value={shot.on_screen_super}
                      onChange={(e) =>
                        updateShot(i, { on_screen_super: e.target.value })
                      }
                      placeholder="Approved headline, CTA, brand, or blank"
                      className="mt-1 text-sm"
                    />
                    <FieldError
                      id={`${fieldId(`shot-${i}-super`)}-error`}
                      message={superErr}
                    />
                  </div>

                  <div>
                    <FieldLabel as="label" htmlFor={fieldId(`shot-${i}-audio`)}>
                      Shot {shotNum} audio cue
                    </FieldLabel>
                    <Input
                      {...a11y(fieldId(`shot-${i}-audio`), audioErr)}
                      value={shot.audio_cue}
                      onChange={(e) =>
                        updateShot(i, { audio_cue: e.target.value })
                      }
                      placeholder="e.g. Warm synth swell"
                      className="mt-1 text-sm"
                    />
                    <FieldError
                      id={`${fieldId(`shot-${i}-audio`)}-error`}
                      message={audioErr}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={addShot}
          disabled={draft.shots.length >= MAX_STORYBOARD_SHOTS}
        >
          <PlusIcon aria-hidden="true" />
          Add shot
        </Button>
      </fieldset>

      {/* Optional director note */}
      <div className="space-y-1.5 border-t border-border pt-4">
        <FieldLabel as="label" htmlFor={fieldId("feedback")}>
          Director note (optional)
        </FieldLabel>
        <Textarea
          id={fieldId("feedback")}
          placeholder="e.g. emphasize warm golden-hour transitions on the hero payoff"
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          rows={2}
          className="text-sm"
        />
      </div>

      {!valid && (
        <p role="status" className="text-sm text-destructive">
          {clientErrorCount > 0
            ? `Fix ${
                clientErrorCount === 1
                  ? "1 storyboard issue"
                  : `${clientErrorCount} storyboard issues`
              } before rendering the video reel.`
            : "Edit the storyboard to address the validation issues above before rendering the video reel."}
        </p>
      )}

      <div className={ACTIONS_ROW}>
        <Button onClick={approve} disabled={!valid}>
          Approve &amp; render video reel
        </Button>
        <ShortcutHint />
      </div>
    </div>
  );
}
