"use client";

import React, { useId, useMemo, useRef, useState } from "react";
import { DownloadIcon, FilmIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import { cn } from "@/lib/utils";
import {
  formatVideoBackendLabel,
  hasVideoFailureMarkers,
  isVideoProductionEnabled,
  parseGeneratedVideos,
  parseIssueList,
  parseVideoStoryboard,
  type GeneratedVideoCut,
  type VideoCutFormat,
} from "@/lib/video-storyboard";

const SAFE_VIDEO_DATA_URL_RE =
  /^data:video\/[a-z0-9.+-]+;base64,[a-z0-9+/=\s]+$/i;

/**
 * Strict URL allowlist for `<video src>` and `<a href download>`: permits only
 * same-origin `/api/gcs?` proxy URLs or base64 `data:video/*` artifact payloads.
 */
export function isSafeVideoUrl(
  url: string | null | undefined
): url is string {
  if (typeof url !== "string" || !url) return false;
  return url.startsWith("/api/gcs?") || SAFE_VIDEO_DATA_URL_RE.test(url);
}

function resolveCutVideoUrl(
  cut: GeneratedVideoCut,
  artifactUrlByName: ReadonlyMap<string, string> | undefined,
  outputUrl: (filename: string) => string | null
): string | null {
  const parsed = parseGsUri(cut.gcs_uri);
  if (parsed) {
    const proxyUrl = gcsProxyUrl(parsed.bucket, parsed.path);
    return isSafeVideoUrl(proxyUrl) ? proxyUrl : null;
  }
  const inline =
    artifactUrlByName?.get(cut.artifact_key) ??
    artifactUrlByName?.get(`video_${cut.cut}.mp4`);
  if (isSafeVideoUrl(inline)) {
    return inline;
  }
  if (cut.gcs_uri_explicitly_empty) {
    return null;
  }
  const fallback = outputUrl(`videos/${cut.cut}.mp4`);
  return isSafeVideoUrl(fallback) ? fallback : null;
}

/**
 * Multi-Cut Video Reel player panel on `/results/[sessionId]`.
 *
 * Renders nothing (`null`) on default static runs where `generated_videos` is
 * absent and Phase 3 did not record failure markers.
 */
export function VideoReelPanel({
  state,
  artifactUrlByName,
  outputUrl,
  className,
}: {
  state: Record<string, unknown>;
  artifactUrlByName: ReadonlyMap<string, string>;
  outputUrl: (filename: string) => string | null;
  className?: string;
}) {
  const uid = useId();
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [selectedCut, setSelectedCut] = useState<VideoCutFormat | null>(null);

  const cuts = useMemo(
    () => parseGeneratedVideos(state.generated_videos),
    [state.generated_videos]
  );
  const storyboard = useMemo(
    () => parseVideoStoryboard(state.video_storyboard),
    [state.video_storyboard]
  );
  const storyboardIssues = useMemo(
    () => parseIssueList(state.video_storyboard__issues),
    [state.video_storyboard__issues]
  );
  const videoIssues = useMemo(
    () => [
      ...parseIssueList(state.generated_videos__issues),
      ...parseIssueList(state.video_qa__issues),
    ],
    [state.generated_videos__issues, state.video_qa__issues]
  );

  const videoEnabled = isVideoProductionEnabled(state.video_production_enabled);
  const failureMarkers = hasVideoFailureMarkers(state);

  if (cuts.length === 0) {
    if (!(videoEnabled && failureMarkers)) {
      return null;
    }

    const unavailableIssues = [...storyboardIssues, ...videoIssues];

    return (
      <section
        aria-label="Multi-cut video reel"
        className={cn(
          "mb-6 rounded-lg border border-mark-pending/40 bg-card p-6 space-y-3",
          className
        )}
      >
        <div className="flex items-center gap-2">
          <FilmIcon
            aria-hidden="true"
            className="h-4 w-4 text-mark-pending"
          />
          <h2 className="text-base font-semibold text-foreground">
            Video reel unavailable for this run
          </h2>
        </div>
        <p className="text-sm text-muted-foreground">
          Phase 3 video production encountered an issue while rendering or
          conforming the commercial cuts. All static proofs and evaluations
          remain complete above.
        </p>
        {unavailableIssues.length > 0 && (
          <ul className="list-disc space-y-1 rounded-md border border-mark-pending/30 bg-mark-pending/5 py-2.5 pr-3 pl-7 text-xs text-muted-foreground">
            {unavailableIssues.map((issue, idx) => (
              <li key={idx}>{issue}</li>
            ))}
          </ul>
        )}
      </section>
    );
  }

  const activeCut = cuts.find((c) => c.cut === selectedCut) ?? cuts[0];
  const activeUrl = resolveCutVideoUrl(
    activeCut,
    artifactUrlByName,
    outputUrl
  );
  const backendLabel = formatVideoBackendLabel(activeCut.backend);

  const handleTabKeyDown = (
    e: React.KeyboardEvent<HTMLButtonElement>,
    index: number
  ) => {
    let nextIdx: number | null = null;
    if (e.key === "ArrowRight") {
      nextIdx = (index + 1) % cuts.length;
    } else if (e.key === "ArrowLeft") {
      nextIdx = (index - 1 + cuts.length) % cuts.length;
    } else if (e.key === "Home") {
      nextIdx = 0;
    } else if (e.key === "End") {
      nextIdx = cuts.length - 1;
    }
    if (nextIdx !== null) {
      e.preventDefault();
      const targetCut = cuts[nextIdx];
      setSelectedCut(targetCut.cut);
      tabRefs.current[nextIdx]?.focus();
    }
  };

  return (
    <section
      aria-label="Multi-cut video reel"
      className={cn(
        "mb-6 rounded-lg border border-border bg-card p-6 space-y-5",
        className
      )}
    >
      {/* Header + Cut tabs */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <FilmIcon aria-hidden="true" className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold text-foreground">
              Multi-cut video reel
            </h2>
          </div>
          {storyboard?.title && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {storyboard.title}
            </p>
          )}
        </div>

        <div
          role="tablist"
          aria-label="Video reel cuts"
          className="inline-flex flex-wrap gap-1 rounded-md border border-border bg-muted/50 p-1"
        >
          {cuts.map((cut, idx) => {
            const isActive = cut.cut === activeCut.cut;
            return (
              <button
                key={cut.cut}
                ref={(el) => {
                  tabRefs.current[idx] = el;
                }}
                id={`${uid}-tab-${cut.cut}`}
                type="button"
                role="tab"
                aria-selected={isActive}
                aria-controls={`${uid}-panel-${cut.cut}`}
                tabIndex={isActive ? 0 : -1}
                onClick={() => setSelectedCut(cut.cut)}
                onKeyDown={(e) => handleTabKeyDown(e, idx)}
                className={cn(
                  "rounded-sm px-3 py-1.5 text-xs font-medium transition-colors tabular-nums",
                  isActive
                    ? "bg-card text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                {cut.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Active Cut Panel */}
      <div
        id={`${uid}-panel-${activeCut.cut}`}
        role="tabpanel"
        aria-labelledby={`${uid}-tab-${activeCut.cut}`}
        className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]"
      >
        {/* Video frame */}
        <div className="flex items-center justify-center rounded-md border border-border bg-background/60 p-4">
          <div
            className={
              activeCut.aspect_ratio === "9:16"
                ? "mx-auto w-full max-w-xs aspect-[9/16]"
                : "w-full max-w-3xl aspect-video"
            }
          >
            {activeUrl ? (
              <video
                key={activeUrl}
                src={activeUrl}
                controls
                playsInline
                preload="metadata"
                aria-label={`${activeCut.label} video player`}
                className="h-full w-full rounded-md border border-border bg-black object-contain"
              />
            ) : (
              <div className="flex h-full w-full items-center justify-center rounded-md border border-dashed border-border bg-muted/30 p-6 text-center text-xs text-muted-foreground">
                Video file unavailable
              </div>
            )}
          </div>
        </div>

        {/* Metadata, QA, Download & Voiceover transcript */}
        <div className="flex flex-col justify-between gap-4 rounded-md border border-border bg-background p-4">
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={cn(
                  "inline-flex items-center rounded-sm border px-2 py-0.5 text-xs font-semibold",
                  activeCut.qa_passed
                    ? "border-mark-pass/40 bg-mark-pass/10 text-mark-pass"
                    : "border-mark-pending/40 bg-mark-pending/10 text-mark-pending"
                )}
              >
                {activeCut.qa_passed ? "QA passed" : "QA flagged"}
              </span>

              <Badge
                variant="secondary"
                className="border border-border bg-card text-xs font-medium text-foreground tabular-nums"
              >
                {activeCut.duration_seconds}s · {activeCut.aspect_ratio}
              </Badge>

              {backendLabel && (
                <Badge
                  variant="secondary"
                  className="border border-border bg-card text-xs font-medium text-foreground"
                >
                  {backendLabel}
                </Badge>
              )}

              {activeCut.shot_numbers.length > 0 && (
                <Badge
                  variant="secondary"
                  className="border border-border bg-card text-xs font-medium text-muted-foreground tabular-nums"
                >
                  Shots {activeCut.shot_numbers.join(", ")}
                </Badge>
              )}
            </div>

            {activeCut.qa_summary && (
              <p className="text-xs text-muted-foreground">
                {activeCut.qa_summary}
              </p>
            )}

            <div className="space-y-1.5 border-t border-border pt-3">
              <FieldLabel>Voiceover transcript</FieldLabel>
              {activeCut.voiceover_transcript ? (
                <p className="text-sm leading-relaxed text-foreground">
                  {activeCut.voiceover_transcript}
                </p>
              ) : (
                <p className="text-xs italic text-muted-foreground">
                  Instrumental only (no spoken voiceover)
                </p>
              )}
            </div>
          </div>

          {activeUrl && (
            <div className="border-t border-border pt-3">
              <a
                href={activeUrl}
                download={`${activeCut.cut}.mp4`}
                className={cn(
                  buttonVariants({ variant: "outline", size: "sm" }),
                  "w-full justify-center"
                )}
              >
                <DownloadIcon aria-hidden="true" />
                Download MP4
              </a>
            </div>
          )}
        </div>
      </div>

      {/* Residual notes if any */}
      {videoIssues.length > 0 && (
        <div className="rounded-md border border-mark-pending/40 bg-mark-pending/5 px-4 py-3 text-xs space-y-1">
          <p className="font-semibold text-mark-pending">Video reel notes</p>
          <ul className="list-disc space-y-0.5 pl-4 text-muted-foreground">
            {videoIssues.map((issue, idx) => (
              <li key={idx}>{issue}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Optional collapsible storyboard summary */}
      {storyboard && (
        <details className="group rounded-md border border-border bg-background">
          <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium text-foreground">
            Storyboard details ({storyboard.shots.length} shots)
          </summary>
          <div className="space-y-3 border-t border-border p-4 text-xs">
            {(storyboard.creative_direction || storyboard.music_direction) && (
              <div className="grid gap-3 sm:grid-cols-2">
                {storyboard.creative_direction && (
                  <div>
                    <FieldLabel>Creative direction</FieldLabel>
                    <p className="mt-0.5 text-foreground/85">
                      {storyboard.creative_direction}
                    </p>
                  </div>
                )}
                {storyboard.music_direction && (
                  <div>
                    <FieldLabel>Music direction</FieldLabel>
                    <p className="mt-0.5 text-foreground/85">
                      {storyboard.music_direction}
                    </p>
                  </div>
                )}
              </div>
            )}

            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {storyboard.shots.map((shot) => (
                <div
                  key={shot.shot_number}
                  className="rounded-sm border border-border bg-card p-3 space-y-1"
                >
                  <div className="flex items-center justify-between gap-2 font-semibold text-foreground tabular-nums">
                    <span>Shot {shot.shot_number}</span>
                    <span className="text-muted-foreground">
                      {shot.duration_seconds}s
                    </span>
                  </div>
                  {shot.anchor_concept_name && (
                    <div className="text-[11px] text-muted-foreground">
                      Anchor: {shot.anchor_concept_name}
                    </div>
                  )}
                  <p className="text-foreground/85">
                    {shot.visual_action_prompt}
                  </p>
                  {shot.voiceover_line && (
                    <p className="italic text-muted-foreground">
                      &ldquo;{shot.voiceover_line}&rdquo;
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        </details>
      )}
    </section>
  );
}
