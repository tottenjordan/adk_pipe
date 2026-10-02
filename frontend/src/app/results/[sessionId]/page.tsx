"use client";

import { useEffect, useState, useCallback, use } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { GcsWidget } from "@/components/gcs-widget";
import { FieldLabel } from "@/components/field-label";
import { getSession, listArtifacts, getArtifact, SELF_USER_ID } from "@/lib/api";
import { fetchEvalReport } from "@/lib/eval-report";
import { gcsProxyUrl } from "@/lib/gcs";
import {
  buildDisplayFields,
  imagesRetryExhausted,
  nonImageWarnings,
  VISUAL_DIRECTION_FIELDS,
  type DisplayFieldDef,
} from "@/lib/utils";
import type { Session } from "@/lib/types";

interface ArtifactData {
  name: string;
  data: unknown;
}

interface EvalVerdict {
  dimension: string;
  score: number;
  verdict: "pass" | "fail";
  rationale: string;
}

interface CreativeScore {
  overall_score: number;
  passed: boolean;
  verdicts: EvalVerdict[];
  strengths: string[];
  improvements: string[];
}

interface AdCopyEvaluation {
  original_id: number;
  headline: string;
  tone_style: string;
  score: CreativeScore;
}

interface VisualConceptEvaluation {
  ad_copy_id: number;
  concept_name: string;
  score: CreativeScore;
}

interface EvalReport {
  brand: string;
  target_product: string;
  target_search_trend: string;
  ad_copy_evaluations: AdCopyEvaluation[];
  visual_concept_evaluations: VisualConceptEvaluation[];
  /** Degradation notes from retry-exhausted pipeline steps (may be absent on older reports). */
  warnings?: string[];
  summary: {
    total_ad_copies: number;
    ad_copies_passed: number;
    avg_ad_copy_score: number;
    total_visual_concepts: number;
    visual_concepts_passed: number;
    avg_visual_score: number;
    overall_pass_rate: number;
    weakest_dimensions: string[];
  };
}

// Visual concept data from session state (final_visual_concepts)
interface VisualConcept {
  ad_copy_id: number;
  concept_name: string;
  trend: string;
  trend_reference: string;
  markets_product: string;
  audience_appeal: string;
  selection_rationale: string;
  headline: string;
  social_caption: string;
  call_to_action: string;
  concept_summary: string;
  image_generation_prompt: string;
}

// Ad copy data from session state (ad_copy_critique)
interface AdCopy {
  original_id: number;
  headline: string;
  body_text: string;
  tone_style: string;
  trend_connection: string;
  audience_appeal_rationale: string;
  social_caption: string;
  call_to_action: string;
  detailed_performance_rationale: string;
}

/** Replicate Python's REMOVE_PUNCTUATION + replace(" ", "_") for image filenames. */
function conceptNameToFilename(name: string): string {
  return name.replace(/[^\w\s]/g, "").replace(/ /g, "_") + ".png";
}

const CAMPAIGN_FIELD_DEFS: DisplayFieldDef[] = [
  { label: "Brand", key: "brand" },
  { label: "Audience", key: "target_audience" },
  { label: "Product", key: "target_product" },
  { label: "Selling points", key: "key_selling_points" },
  { label: "Trend", key: "target_search_trends" },
];

export default function ResultsPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = use(params);
  const searchParams = useSearchParams();
  const appName = searchParams.get("app") || "trend_scout";
  const userId = searchParams.get("userId") || SELF_USER_ID;

  const [session, setSession] = useState<Session | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [evalReport, setEvalReport] = useState<EvalReport | null>(null);
  const [evalStatus, setEvalStatus] = useState<
    "idle" | "loading" | "pending" | "error"
  >("idle");
  const [stateOpen, setStateOpen] = useState(false);
  const [artifactsOpen, setArtifactsOpen] = useState(false);

  // The eval report is the LAST artifact the run writes to GCS, so it can 404 if the
  // results page opens before the run's final write lands. fetchEvalReport retries on
  // 404 and reports "pending" so the UI can offer a refresh instead of silently
  // showing nothing (see lib/eval-report.ts).
  const loadEval = useCallback(async (sess: Session) => {
    const folder = sess.state?.gcs_folder as string;
    const subdir = sess.state?.agent_output_dir as string;
    const bucket =
      (sess.state?.gcs_bucket_name as string) ||
      (sess.state?.gcs_bucket as string)?.replace(/^gs:\/\//, "") ||
      "";
    if (!(bucket && folder && subdir)) return;

    setEvalStatus("loading");
    const evalUrl = gcsProxyUrl(bucket, `${folder}/${subdir}/creative_eval_report.json`);
    const result = await fetchEvalReport<EvalReport>(evalUrl);
    if (result.status === "found") {
      setEvalReport(result.report);
      setEvalStatus("idle");
    } else {
      setEvalStatus(result.status);
    }
  }, []);

  useEffect(() => {
    async function load() {
      try {
        const [sess, artifactNames] = await Promise.all([
          getSession(appName, userId, sessionId),
          listArtifacts(appName, userId, sessionId),
        ]);
        setSession(sess);

        const loaded = await Promise.all(
          artifactNames.map(async (name) => {
            try {
              const data = await getArtifact(appName, userId, sessionId, name);
              return { name, data };
            } catch {
              return { name, data: null };
            }
          })
        );
        setArtifacts(loaded);
        setLoading(false);

        // Fetch eval report (retries on 404 for the last-written-artifact race).
        await loadEval(sess);
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load results"
        );
        setLoading(false);
      }
    }
    load();
  }, [appName, userId, sessionId, loadEval]);

  if (loading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center py-24">
        <div className="flex space-x-1.5 mb-4">
          <div className="h-2.5 w-2.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: "0ms" }} />
          <div className="h-2.5 w-2.5 rounded-full bg-primary/70 animate-bounce" style={{ animationDelay: "150ms" }} />
          <div className="h-2.5 w-2.5 rounded-full bg-primary/40 animate-bounce" style={{ animationDelay: "300ms" }} />
        </div>
        <p className="text-sm text-muted-foreground">Loading results...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <div className="rounded-lg border border-border bg-card p-6">
          <p className="text-mark-fail">{error}</p>
          <Link href="/">
            <Button variant="outline" className="mt-4">
              Back to home
            </Button>
          </Link>
        </div>
      </div>
    );
  }

  const state = session?.state || {};
  const noImages = imagesRetryExhausted(state);
  // Degradation warnings from the eval report, minus the image-exhaustion note
  // (already surfaced by the dedicated zero-image banner above) — what remains is
  // the research-producer exhaustions worth flagging on their own.
  const degradationWarnings = nonImageWarnings(evalReport?.warnings);
  const gcsUri = [state.gcs_bucket, state.gcs_folder, state.agent_output_dir]
    .filter(Boolean)
    .join("/");

  const bucketName =
    (state.gcs_bucket_name as string) ||
    (state.gcs_bucket as string)?.replace(/^gs:\/\//, "") ||
    "";
  const folder = state.gcs_folder as string;
  const subdir = state.agent_output_dir as string;

  const galleryUrl =
    bucketName && folder && subdir
      ? gcsProxyUrl(bucketName, `${folder}/${subdir}/creative_portfolio_gallery.html`)
      : null;

  // Extract visual concepts and ad copies from session state
  const visualConceptsRaw = state.final_visual_concepts as { visual_concepts?: VisualConcept[] } | undefined;
  const visualConcepts: VisualConcept[] = visualConceptsRaw?.visual_concepts || [];

  const adCopyRaw = state.ad_copy_critique as { ad_copies?: AdCopy[] } | undefined;
  const adCopies: AdCopy[] = adCopyRaw?.ad_copies || [];

  // Build image URL for a visual concept
  function getImageUrl(conceptName: string): string | null {
    if (!bucketName || !folder || !subdir) return null;
    const filename = conceptNameToFilename(conceptName);
    return gcsProxyUrl(bucketName, `${folder}/${subdir}/${filename}`);
  }

  // Find matching eval for a visual concept
  function findVisualEval(conceptName: string): VisualConceptEvaluation | undefined {
    return evalReport?.visual_concept_evaluations.find(
      (ve) => ve.concept_name === conceptName
    );
  }

  // Find matching ad copy eval for a visual concept.
  // Try by ad_copy_id → original_id first, then by headline match, then by index.
  function findAdCopyEvalForVisual(vc: VisualConcept, vcIndex: number): AdCopyEvaluation | undefined {
    if (!evalReport) return undefined;
    const evals = evalReport.ad_copy_evaluations;
    // 1. Match by ID
    const byId = evals.find((ae) => ae.original_id === vc.ad_copy_id);
    if (byId) return byId;
    // 2. Match by headline (visual concept carries the ad copy headline)
    const byHeadline = evals.find((ae) => ae.headline === vc.headline);
    if (byHeadline) return byHeadline;
    // 3. Fall back to index position
    if (vcIndex < evals.length) return evals[vcIndex];
    return undefined;
  }

  // Find matching ad copy data from session state for a visual concept.
  function findAdCopyForVisual(vc: VisualConcept, vcIndex: number): AdCopy | undefined {
    // 1. Match by ID
    const byId = adCopies.find((ac) => ac.original_id === vc.ad_copy_id);
    if (byId) return byId;
    // 2. Match by headline
    const byHeadline = adCopies.find((ac) => ac.headline === vc.headline);
    if (byHeadline) return byHeadline;
    // 3. Fall back to index position
    if (vcIndex < adCopies.length) return adCopies[vcIndex];
    return undefined;
  }

  const imageArtifacts = artifacts.filter(
    (a) => a.name.endsWith(".png") || a.name.endsWith(".jpg")
  );
  const pdfArtifacts = artifacts.filter((a) => a.name.endsWith(".pdf"));
  const htmlArtifacts = artifacts.filter((a) => a.name.endsWith(".html"));
  const otherArtifacts = artifacts.filter(
    (a) =>
      !a.name.endsWith(".png") &&
      !a.name.endsWith(".jpg") &&
      !a.name.endsWith(".pdf") &&
      !a.name.endsWith(".html")
  );

  // Campaign metadata fields
  const campaignFields = buildDisplayFields(state, CAMPAIGN_FIELD_DEFS);

  // Optional user visual art-direction inputs (PR #114) — hidden when unset
  const visualDirectionFields = buildDisplayFields(state, VISUAL_DIRECTION_FIELDS);

  // Does this run have the creative asset + eval view?
  const hasCreativeView = (appName === "creative_agent" || appName === "interactive_creative") && visualConcepts.length > 0;

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-8">
      {/* Zero-image warning: the image producer exhausted all retries (issue #116),
          so this run shipped no visuals. Surface it prominently — otherwise the
          only signal is an easy-to-miss eval-report note and empty image tabs. */}
      {noImages && (
        <div className="mb-6 rounded-lg border border-mark-pending/40 bg-mark-pending/10 px-5 py-4">
          <h2 className="text-sm font-semibold text-mark-pending">
            No images were generated for this run
          </h2>
          <p className="mt-0.5 text-xs text-foreground">
            The image model failed repeatedly (MALFORMED_FUNCTION_CALL), so no
            visual concepts were rendered. This is usually transient — start a new
            run of the same campaign to generate visuals.
          </p>
        </div>
      )}

      {/* Header row */}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">
            Results
          </h1>
          <p className="mt-1 text-sm text-muted-foreground font-mono">
            {appName} / {sessionId}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {galleryUrl && (
            <Button
              variant="outline"
              size="sm"
              className="text-xs"
              onClick={() => window.open(galleryUrl, "_blank")}
            >
              Open portfolio gallery
            </Button>
          )}
          <Link href="/">
            <Button variant="outline" size="sm">
              New run
            </Button>
          </Link>
        </div>
      </div>

      {/* Campaign metadata bar — equal-width, centered */}
      <div className="-mx-6 px-6 pt-4 pb-3 mb-6 border-b border-border">
        <div className="grid gap-3 justify-center" style={{ gridTemplateColumns: `repeat(${campaignFields.length + (gcsUri ? 1 : 0)}, minmax(0, 240px))` }}>
          {campaignFields.map((f) => (
            <dl key={f.key} className="rounded-lg border border-border bg-card px-4 py-2.5 text-center">
              <FieldLabel as="dt">{f.label}</FieldLabel>
              <dd className="mt-0.5 text-sm font-medium leading-snug break-words text-foreground">
                {f.value}
              </dd>
            </dl>
          ))}
          {gcsUri && <GcsWidget uri={gcsUri} />}
        </div>
      </div>

      {/* Visual Direction bar — optional user art direction (PR #114) */}
      {visualDirectionFields.length > 0 && (
        <div className="-mx-6 px-6 pb-3 mb-6 border-b border-border">
          <h2 className="mb-2 text-center text-sm font-semibold text-foreground">
            Visual direction
          </h2>
          <div className="grid gap-3 justify-center" style={{ gridTemplateColumns: `repeat(${visualDirectionFields.length}, minmax(0, 240px))` }}>
            {visualDirectionFields.map((f) => (
              <dl key={f.key} className="rounded-lg border border-border bg-card px-4 py-2.5 text-center">
                <FieldLabel as="dt">{f.label}</FieldLabel>
                <dd className="mt-0.5 text-sm font-medium leading-snug break-words break-all text-foreground">
                  {f.value}
                </dd>
              </dl>
            ))}
          </div>
        </div>
      )}

      {/* Eval still generating / failed — offer a manual refresh (race: the report
          is the last artifact written, so it can lag the page load). */}
      {!evalReport &&
        (evalStatus === "loading" ||
          evalStatus === "pending" ||
          evalStatus === "error") && (
          <div className="rounded-lg border border-border bg-card mb-6 px-5 py-4 flex items-center justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold text-foreground">
                Creative evaluation
              </h2>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {evalStatus === "loading" &&
                  "Loading evaluation report…"}
                {evalStatus === "pending" &&
                  "The evaluation report is still being generated — it's the final step of the run. Give it a moment, then refresh."}
                {evalStatus === "error" &&
                  "Couldn't load the evaluation report."}
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              className="text-xs shrink-0"
              disabled={evalStatus === "loading" || !session}
              onClick={() => session && loadEval(session)}
            >
              {evalStatus === "loading" ? "Loading…" : "Refresh"}
            </Button>
          </div>
        )}

      {/* Degraded-research warning: one or more research producers exhausted their
          retries and shipped no output, so the creative brief was built on partial
          research. Distinct from (and filtered against) the zero-image banner. */}
      {degradationWarnings.length > 0 && (
        <div className="mb-6 rounded-lg border border-mark-pending/40 bg-mark-pending/10 px-5 py-4">
          <h2 className="text-sm font-semibold text-mark-pending">
            Some research steps produced no output
          </h2>
          <p className="mt-0.5 text-xs text-foreground">
            Parts of the pipeline exhausted their retries, so this run&apos;s
            creative was built on incomplete research:
          </p>
          <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs text-foreground">
            {degradationWarnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Eval summary header */}
      {evalReport && (
        <div className="rounded-lg border border-border bg-card mb-6 px-5 py-4 space-y-3">
          <h2 className="text-sm font-semibold text-foreground">
            Creative evaluation
          </h2>
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-md bg-muted/60 px-4 py-3 text-center">
              <FieldLabel as="p">Overall pass rate</FieldLabel>
              <p className={`text-2xl font-bold tabular-nums ${evalReport.summary.overall_pass_rate >= 0.7 ? "text-mark-pass" : "text-mark-fail"}`}>
                {(evalReport.summary.overall_pass_rate * 100).toFixed(0)}%
              </p>
            </div>
            <div className="rounded-md bg-muted/60 px-4 py-3 text-center">
              <FieldLabel as="p">Ad copies</FieldLabel>
              <p className="text-lg font-semibold text-foreground tabular-nums">
                {evalReport.summary.ad_copies_passed}/{evalReport.summary.total_ad_copies} passed
              </p>
              <p className="text-xs text-muted-foreground">
                avg {evalReport.summary.avg_ad_copy_score.toFixed(2)}
              </p>
            </div>
            <div className="rounded-md bg-muted/60 px-4 py-3 text-center">
              <FieldLabel as="p">Visual concepts</FieldLabel>
              <p className="text-lg font-semibold text-foreground tabular-nums">
                {evalReport.summary.visual_concepts_passed}/{evalReport.summary.total_visual_concepts} passed
              </p>
              <p className="text-xs text-muted-foreground">
                avg {evalReport.summary.avg_visual_score.toFixed(2)}
              </p>
            </div>
          </div>
          {evalReport.summary.weakest_dimensions.length > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <FieldLabel>Weakest dimensions:</FieldLabel>
              {evalReport.summary.weakest_dimensions.map((d) => (
                <Badge
                  key={d}
                  variant="secondary"
                  className="bg-mark-fail/10 text-mark-fail border-0 text-[10px]"
                >
                  {d.replace(/_/g, " ")}
                </Badge>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Creative rows: image + eval per visual concept */}
      {hasCreativeView && (
        <div className="space-y-6 mb-6">
          {visualConcepts.map((vc, i) => {
            const imgUrl = getImageUrl(vc.concept_name);
            const vcEval = findVisualEval(vc.concept_name);
            const acEval = findAdCopyEvalForVisual(vc, i);
            const ac = findAdCopyForVisual(vc, i);

            return (
              <div
                key={i}
                className="rounded-lg border border-border bg-card overflow-hidden"
              >
                <div className="grid lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                  {/* Left: gallery-style image card */}
                  <div className="flex flex-col">
                    {/* Headline */}
                    <div className="px-5 py-3 border-b border-border">
                      <h3 className="text-lg font-semibold text-foreground">
                        {vc.headline}
                      </h3>
                      <p className="text-xs text-muted-foreground mt-0.5">
                        {vc.concept_name}
                      </p>
                    </div>

                    {/* Image with hover overlay — fixed 480px square (mirrors the
                        HTML gallery) so it stays a consistent size regardless of
                        window width instead of stretching to the eval column. */}
                    <div className="relative group overflow-hidden aspect-square w-full max-w-[480px] mx-auto">
                      {imgUrl ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={imgUrl}
                          alt={vc.concept_summary}
                          className="w-full h-full object-cover transition-transform duration-300 group-hover:scale-110"
                        />
                      ) : (
                        <div className="flex items-center justify-center h-full w-full bg-muted/50 text-sm text-muted-foreground">
                          No image available
                        </div>
                      )}

                      {/* Hover overlay — 4 corners like the gallery HTML */}
                      <div className="absolute inset-0 bg-black/70 opacity-0 group-hover:opacity-100 transition-opacity duration-300 pointer-events-none grid grid-cols-2 grid-rows-2 p-5 gap-3">
                        <div className="self-start justify-self-start text-white text-sm leading-snug">
                          <span className="block text-white/75 font-semibold text-xs mb-1">Trend reference</span>
                          {vc.trend_reference}
                        </div>
                        <div className="self-start justify-self-end text-right text-white text-sm leading-snug">
                          <span className="block text-white/75 font-semibold text-xs mb-1">Visual concept</span>
                          {vc.concept_summary}
                        </div>
                        <div className="self-end justify-self-start text-white text-sm leading-snug">
                          <span className="block text-white/75 font-semibold text-xs mb-1">Markets product</span>
                          {vc.markets_product}
                        </div>
                        <div className="self-end justify-self-end text-right text-white text-sm leading-snug">
                          <span className="block text-white/75 font-semibold text-xs mb-1">Audience appeal</span>
                          {vc.audience_appeal}
                        </div>
                      </div>
                    </div>

                    {/* Caption */}
                    <div className="px-5 py-3 border-t border-border">
                      <p className="text-sm text-foreground/80">{vc.social_caption}</p>
                    </div>
                  </div>

                  {/* Right: eval scores */}
                  <div className="border-l border-border p-5 space-y-4 overflow-y-auto max-h-[700px]">
                    {/* Ad copy details + eval */}
                    {ac && (
                      <div className="space-y-3">
                        <div className="flex items-center justify-between">
                          <h4 className="text-sm font-semibold text-foreground">
                            Ad copy
                          </h4>
                          {acEval && (
                            <Badge
                              variant="secondary"
                              className={`border-0 text-xs tabular-nums ${acEval.score.passed ? "bg-mark-pass/10 text-mark-pass" : "bg-mark-fail/10 text-mark-fail"}`}
                            >
                              {acEval.score.passed ? "Pass" : "Fail"} &middot; {(acEval.score.overall_score * 100).toFixed(0)}%
                            </Badge>
                          )}
                        </div>

                        {/* Ad copy content */}
                        <div className="rounded-md bg-muted/60 p-3 space-y-2">
                          <p className="font-semibold text-sm text-foreground">{ac.headline}</p>
                          <p className="text-xs text-foreground/85 leading-relaxed">{ac.body_text}</p>
                          <div className="flex items-center gap-2 flex-wrap">
                            <Badge variant="outline" className="text-[10px]">{ac.tone_style}</Badge>
                            <span className="text-xs text-muted-foreground">CTA: {ac.call_to_action}</span>
                          </div>
                        </div>

                        {/* Ad copy eval verdicts */}
                        {acEval && (
                          <div className="grid grid-cols-2 gap-1.5">
                            {acEval.score.verdicts.map((v) => (
                              <div key={v.dimension} className="rounded-md bg-muted/60 px-2.5 py-1.5">
                                <div className="flex items-center justify-between mb-0.5">
                                  <FieldLabel className="first-letter:uppercase">
                                    {v.dimension.replace(/_/g, " ")}
                                  </FieldLabel>
                                  <span className={`text-xs font-bold tabular-nums ${v.score >= 7 ? "text-mark-pass" : "text-mark-fail"}`}>
                                    {v.score}/10
                                  </span>
                                </div>
                                <p className="text-xs text-muted-foreground leading-snug">
                                  {v.rationale}
                                </p>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {/* Divider */}
                    {ac && vcEval && <div className="border-t border-border" />}

                    {/* Visual concept eval */}
                    {vcEval && (
                      <div className="space-y-3">
                        <div className="flex items-center justify-between">
                          <h4 className="text-sm font-semibold text-foreground">
                            Visual concept
                          </h4>
                          <Badge
                            variant="secondary"
                            className={`border-0 text-xs tabular-nums ${vcEval.score.passed ? "bg-mark-pass/10 text-mark-pass" : "bg-mark-fail/10 text-mark-fail"}`}
                          >
                            {vcEval.score.passed ? "Pass" : "Fail"} &middot; {(vcEval.score.overall_score * 100).toFixed(0)}%
                          </Badge>
                        </div>

                        <div className="grid grid-cols-2 gap-1.5">
                          {vcEval.score.verdicts.map((v) => (
                            <div key={v.dimension} className="rounded-md bg-muted/60 px-2.5 py-1.5">
                              <div className="flex items-center justify-between mb-0.5">
                                <FieldLabel className="first-letter:uppercase">
                                  {v.dimension.replace(/_/g, " ")}
                                </FieldLabel>
                                <span className={`text-xs font-bold tabular-nums ${v.score >= 7 ? "text-mark-pass" : "text-mark-fail"}`}>
                                  {v.score}/10
                                </span>
                              </div>
                              <p className="text-xs text-muted-foreground leading-snug">
                                {v.rationale}
                              </p>
                            </div>
                          ))}
                        </div>

                        {/* Strengths & Improvements (combined from both evals) */}
                        <div className="grid grid-cols-2 gap-3 text-xs">
                          {vcEval.score.strengths.length > 0 && (
                            <div>
                              <p className="font-medium text-foreground mb-1">Strengths</p>
                              <ul className="space-y-1 text-muted-foreground">
                                {vcEval.score.strengths.map((s, j) => (
                                  <li key={j} className="leading-snug text-xs">&bull; {s}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {vcEval.score.improvements.length > 0 && (
                            <div>
                              <p className="font-medium text-foreground mb-1">Improvements</p>
                              <ul className="space-y-1 text-muted-foreground">
                                {vcEval.score.improvements.map((s, j) => (
                                  <li key={j} className="leading-snug text-xs">&bull; {s}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {/* Fallback when no eval data */}
                    {!acEval && !vcEval && (
                      <p className="text-xs text-muted-foreground italic">
                        No evaluation data available for this creative.
                      </p>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Artifacts collapsible */}
      {artifacts.length > 0 && (
        <Collapsible open={artifactsOpen} onOpenChange={setArtifactsOpen}>
          <div className="rounded-lg border border-border bg-card mb-6 overflow-hidden">
            <CollapsibleTrigger className="w-full">
              <div className="cursor-pointer hover:bg-muted/60 transition-colors px-5 py-3 flex items-center justify-between">
                <span className="text-sm font-semibold text-foreground flex items-center gap-2">
                  Artifacts
                  <Badge
                    variant="secondary"
                    className="text-[10px] tabular-nums"
                  >
                    {artifacts.length}
                  </Badge>
                </span>
                <span className="text-xs text-primary">
                  {artifactsOpen ? "Collapse" : "Expand"}
                </span>
              </div>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <div className="px-5 pb-5">
                <Tabs defaultValue="images">
                  <TabsList>
                    {imageArtifacts.length > 0 && (
                      <TabsTrigger value="images">
                        Images ({imageArtifacts.length})
                      </TabsTrigger>
                    )}
                    {pdfArtifacts.length > 0 && (
                      <TabsTrigger value="pdfs">
                        PDFs ({pdfArtifacts.length})
                      </TabsTrigger>
                    )}
                    {htmlArtifacts.length > 0 && (
                      <TabsTrigger value="html">
                        HTML ({htmlArtifacts.length})
                      </TabsTrigger>
                    )}
                    {otherArtifacts.length > 0 && (
                      <TabsTrigger value="other">
                        Other ({otherArtifacts.length})
                      </TabsTrigger>
                    )}
                  </TabsList>

                  <TabsContent value="images">
                    <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
                      {imageArtifacts.map((a) => {
                        const src =
                          bucketName && folder && subdir
                            ? gcsProxyUrl(bucketName, `${folder}/${subdir}/${a.name}`)
                            : null;
                        return (
                          <div
                            key={a.name}
                            className="overflow-hidden rounded-md border border-border bg-background"
                          >
                            {src ? (
                              // eslint-disable-next-line @next/next/no-img-element
                              <img
                                src={src}
                                alt={a.name}
                                className="aspect-square w-full object-cover"
                              />
                            ) : (
                              <div className="flex aspect-square items-center justify-center bg-muted/50 text-xs text-muted-foreground">
                                No preview
                              </div>
                            )}
                            <p className="truncate px-3 py-2 font-mono text-[10px] text-muted-foreground">
                              {a.name}
                            </p>
                          </div>
                        );
                      })}
                    </div>
                  </TabsContent>

                  <TabsContent value="pdfs">
                    <ul className="space-y-2">
                      {pdfArtifacts.map((a) => (
                        <li
                          key={a.name}
                          className="flex items-center justify-between rounded-md bg-muted/60 px-4 py-3"
                        >
                          <span className="text-sm font-mono text-foreground/85">
                            {a.name}
                          </span>
                          <Badge
                            variant="outline"
                          >
                            PDF
                          </Badge>
                        </li>
                      ))}
                    </ul>
                  </TabsContent>

                  <TabsContent value="html">
                    <ul className="space-y-2">
                      {htmlArtifacts.map((a) => (
                        <li
                          key={a.name}
                          className="flex items-center justify-between rounded-md bg-muted/60 px-4 py-3"
                        >
                          <span className="text-sm font-mono text-foreground/85">
                            {a.name}
                          </span>
                          <Badge
                            variant="outline"
                          >
                            HTML
                          </Badge>
                        </li>
                      ))}
                    </ul>
                  </TabsContent>

                  <TabsContent value="other">
                    <ul className="space-y-2">
                      {otherArtifacts.map((a) => (
                        <li
                          key={a.name}
                          className="rounded-md bg-muted/60 px-4 py-3 text-sm font-mono text-foreground/85"
                        >
                          {a.name}
                        </li>
                      ))}
                    </ul>
                  </TabsContent>
                </Tabs>
              </div>
            </CollapsibleContent>
          </div>
        </Collapsible>
      )}

      {/* Raw session state */}
      <Collapsible open={stateOpen} onOpenChange={setStateOpen}>
        <div className="rounded-lg border border-border bg-card overflow-hidden">
          <CollapsibleTrigger className="w-full">
            <div className="cursor-pointer hover:bg-muted/60 transition-colors px-5 py-3 flex items-center justify-between">
              <span className="text-sm font-semibold text-foreground">
                Session state
              </span>
              <span className="text-xs text-primary">
                {stateOpen ? "Collapse" : "Expand"}
              </span>
            </div>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="px-5 pb-5">
              <pre className="max-h-96 overflow-auto rounded-md bg-muted/60 p-4 text-xs font-mono text-foreground/85">
                {JSON.stringify(state, null, 2)}
              </pre>
            </div>
          </CollapsibleContent>
        </div>
      </Collapsible>
    </div>
  );
}
