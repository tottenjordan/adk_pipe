"use client";

import { useEffect, useState, useCallback, useMemo, useRef, use } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import { Collapsible } from "@/components/ui/collapsible";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getArtifact,
  getSession,
  listArtifacts,
  fetchRunChangelog,
  claimRunHandoff,
  deliveryKitUrl as buildDeliveryKitUrl,
  SELF_USER_ID,
  type GenerateVariationsResponse,
  type RunChangelog,
} from "@/lib/api";
import { fetchEvalReport, parseEvalReport } from "@/lib/eval-report";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import { outputDir } from "@/lib/results-paths";
import { classifyWarnings } from "@/lib/run-warnings";
import { parseGeneratedVideos } from "@/lib/video-storyboard";
import { LearningSummary } from "@/components/learning-summary";
import { TrendSituationLine } from "@/components/trend-situation-line";
import { SaferAngleLine } from "@/components/safer-angle-line";
import {
  buildDisplayFields,
  imagesRetryExhausted,
  RATING_LEARNING_FIELD,
  SAFER_ANGLE_FIELD,
  VISUAL_DIRECTION_FIELDS,
  type DisplayFieldDef,
} from "@/lib/utils";
import type { Part, Session } from "@/lib/types";
import {
  buildProofs,
  conceptNameToFilename,
  sortProofs,
  type AdCopy,
  type EvalReport,
  type Proof,
  type ProofSort,
  type VisualConcept,
} from "@/lib/eval-matching";
import { campaignSummary } from "@/lib/results-copy";
import { imagesNotRendered, sessionStoppedEarly } from "@/lib/run-completion";
import { CreativeBrief } from "@/components/creative-brief";
import { parseCreativeBrief } from "@/lib/creative-brief";
import type { ReportSources } from "@/lib/research-report";
import {
  DisclosureChevron,
  DisclosurePanel,
  DisclosureTrigger,
  QuietDisclosure,
} from "@/components/quiet-disclosure";
import { ArtifactsPanel, type ArtifactData } from "./artifacts-panel";
import { ProofDetail } from "./proof-detail";
import { ProofGrid } from "./proof-grid";
import { CreativeRatings, useSessionRatings } from "./rating-control";
import { isProofRated } from "@/lib/ratings";
import { ResultsHeader } from "./results-header";
import { ResultsSummary, type EvalStatus } from "./results-summary";
import { RunChangelogPanel } from "./run-changelog";
import { VariationDrawer } from "./variation-drawer";
import { VideoReelPanel } from "./video-reel-panel";
import { ShareDialog } from "@/components/share-dialog";
import { SessionShares } from "@/components/shares-list";
import { failedChecksFor, hasRenderedImage, type Share } from "@/lib/shares";
import { canPersonalise } from "@/lib/variants";
import { useWallClock } from "@/lib/use-wall-clock";
import {
  PersonalisePanel,
  personalisationProps,
  usePersonalisationData,
} from "@/components/personalise-panel";

// Heavy, rarely needed on first paint: the report (react-markdown + remark-gfm) and the
// Deploy panel (bandit scenario code) each mount only when their disclosure opens.
const ResearchReport = dynamic(
  () => import("@/components/research-report").then((m) => m.ResearchReport),
  { loading: () => <p className="text-sm text-muted-foreground">Loading report…</p> }
);
const DeployPanel = dynamic(() => import("./deploy-panel").then((m) => m.DeployPanel), {
  loading: () => <p className="text-sm text-muted-foreground">Loading deploy options…</p>,
});

/**
 * The experiment set-up, collapsed by default below the review sections: a one-line
 * summary, and the lazily loaded Deploy panel once opened (the panel unmounts its
 * children while closed, so the Deploy panel's chunk loads on first open).
 */
function DeployDisclosure({ children }: { children: React.ReactNode }) {
  return (
    <section aria-labelledby="deploy-heading" className="mb-4 overflow-hidden rounded-lg border border-border bg-card">
      <Collapsible>
        <h2 id="deploy-heading" className="text-sm font-semibold text-foreground">
          <DisclosureTrigger className="flex w-full items-center justify-between gap-3 px-4 pt-2.5 text-left transition-colors hover:text-primary">
            Deploy creatives as a live experiment
            <DisclosureChevron className="size-4" />
          </DisclosureTrigger>
        </h2>
        <p className="px-4 pb-2.5 text-xs text-muted-foreground">
          Test 2–4 of these creatives on simulated readers; a bandit learns which works for whom.
        </p>
        <DisclosurePanel>
          <div className="border-t border-border px-4 py-4">{children}</div>
        </DisclosurePanel>
      </Collapsible>
    </section>
  );
}

/** Layout-matched loading state: a header line and four contact-sheet card shells. */
function ResultsSkeleton() {
  return (
    <div role="status" className="mx-auto max-w-[1600px] px-6 py-6">
      <span className="sr-only">Loading results…</span>
      <Skeleton className="mb-2 h-7 w-48" />
      <Skeleton className="mb-6 h-4 w-96 max-w-full" />
      <Skeleton className="mb-3 h-4 w-28" />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} data-slot="skeleton-card" aria-hidden className="rounded-lg border border-border bg-card p-2">
            <Skeleton className="aspect-square w-full rounded-none" />
            <div className="px-1 pt-3 pb-1">
              <Skeleton className="h-5 w-4/5" />
              <Skeleton className="mt-2 h-3 w-1/2" />
              <div className="mt-4 grid grid-cols-2 gap-3 border-t border-border pt-3">
                <Skeleton className="h-8 w-14" />
                <Skeleton className="h-8 w-14" />
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const CAMPAIGN_FIELD_DEFS: DisplayFieldDef[] = [
  { label: "Brand", key: "brand" },
  { label: "Audience", key: "target_audience" },
  { label: "Product", key: "target_product" },
  { label: "Selling points", key: "key_selling_points" },
  { label: "Trend", key: "target_search_trends" },
  RATING_LEARNING_FIELD,
  SAFER_ANGLE_FIELD,
];

const EMPTY_STATE: Record<string, unknown> = {};

function parseStateEvalReport(raw: unknown): EvalReport | null {
  let obj = raw;
  if (typeof obj === "string") {
    try {
      obj = JSON.parse(obj);
    } catch {
      return null;
    }
  }
  return parseEvalReport(obj);
}

function countStateVariations(raw: unknown): number {
  let obj = raw;
  if (typeof obj === "string") {
    try {
      obj = JSON.parse(obj);
    } catch {
      return 0;
    }
  }
  return Array.isArray(obj) ? obj.length : 0;
}

function extractVideoArtifactDataUrl(part: Part | null | undefined): string | null {
  const inline = part?.inlineData;
  if (!inline || typeof inline.mimeType !== "string" || typeof inline.data !== "string") {
    return null;
  }
  const mime = inline.mimeType.trim().toLowerCase();
  const data = inline.data.trim();
  if (!mime.startsWith("video/") || !data || !/^[A-Za-z0-9+/=_-]+$/.test(data)) {
    return null;
  }
  return `data:${mime};base64,${data}`;
}

function neededVideoArtifactNames(
  sessionState: Record<string, unknown> | undefined,
  artifactNames: readonly string[]
): string[] {
  if (!sessionState || artifactNames.length === 0) return [];
  const cuts = parseGeneratedVideos(sessionState.generated_videos);
  if (cuts.length === 0) return [];
  const available = new Set(artifactNames);
  const needed = new Set<string>();
  for (const cut of cuts) {
    if (parseGsUri(cut.gcs_uri)) continue;
    for (const key of [cut.artifact_key, `video_${cut.cut}.mp4`]) {
      if (key && available.has(key)) needed.add(key);
    }
  }
  return [...needed];
}

async function hydrateVideoArtifactUrls(
  appName: string,
  userId: string,
  sessionId: string,
  artifactNames: readonly string[],
  sessionState: Record<string, unknown> | undefined
): Promise<Map<string, string>> {
  const names = neededVideoArtifactNames(sessionState, artifactNames);
  const map = new Map<string, string>();
  if (names.length === 0) return map;
  await Promise.all(
    names.map(async (name) => {
      try {
        const part = await getArtifact(appName, userId, sessionId, name);
        const url = extractVideoArtifactDataUrl(part);
        if (url) map.set(name, url);
      } catch {
        // Non-fatal: VideoReelPanel falls back to GCS proxy or unavailable notice.
      }
    })
  );
  return map;
}

export default function ResultsPage({ params }: PageProps<"/results/[sessionId]">) {
  const { sessionId } = use(params);
  const searchParams = useSearchParams();
  const router = useRouter();
  const appName = searchParams.get("app") || "trend_scout";
  const userId = searchParams.get("userId") || SELF_USER_ID;
  const fromGe = searchParams.get("from") === "ge";
  const handoffToken = searchParams.get("handoff") || "";

  const [session, setSession] = useState<Session | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactData[]>([]);
  const [videoArtifactUrls, setVideoArtifactUrls] = useState<ReadonlyMap<string, string>>(
    () => new Map()
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Bumped by "Try again" to re-run the load effect.
  const [attempt, setAttempt] = useState(0);
  const [evalReport, setEvalReport] = useState<EvalReport | null>(null);
  const [evalStatus, setEvalStatus] = useState<EvalStatus>("idle");
  const [sort, setSort] = useState<ProofSort>("pipeline");
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailIndex, setDetailIndex] = useState<number | null>(null);
  const [variationTarget, setVariationTarget] = useState<Proof | null>(null);
  const runKey = `${appName}:${userId}:${sessionId}`;
  const [changelogState, setChangelogState] = useState<{
    key: string;
    data: RunChangelog | null;
    loading: boolean;
    error: string | null;
  }>({ key: "", data: null, loading: false, error: null });
  const changelog = changelogState.key === runKey ? changelogState.data : null;
  const changelogLoading = changelogState.key === runKey ? changelogState.loading : false;
  const changelogError = changelogState.key === runKey ? changelogState.error : null;
  const changelogFetchedKeyRef = useRef<string | null>(null);
  const changelogAbortRef = useRef<AbortController | null>(null);
  const proofButtons = useRef(new Map<number, HTMLButtonElement>());
  // Links created on this page since load (newest first), shown in "Shared links".
  const [newShares, setNewShares] = useState<Share[]>([]);
  const onShareCreated = useCallback((s: Share) => setNewShares((prev) => [s, ...prev]), []);
  // Wall clock for the timely creative's expiry (lib/timely.ts), refreshed every
  // minute so "Timely" turns "Expired" while the page is open.
  const now = useWallClock();

  const loadChangelog = useCallback(async () => {
    if (changelogFetchedKeyRef.current === runKey) return;
    changelogFetchedKeyRef.current = runKey;
    changelogAbortRef.current?.abort();
    const controller = new AbortController();
    changelogAbortRef.current = controller;
    setChangelogState({ key: runKey, data: null, loading: true, error: null });
    try {
      const data = await fetchRunChangelog(appName, userId, sessionId, {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setChangelogState({ key: runKey, data, loading: false, error: null });
    } catch (err) {
      if (
        controller.signal.aborted ||
        (err instanceof Error && err.name === "AbortError")
      ) {
        return;
      }
      if (changelogFetchedKeyRef.current === runKey) {
        changelogFetchedKeyRef.current = null;
      }
      setChangelogState({
        key: runKey,
        data: null,
        loading: false,
        error: err instanceof Error ? err.message : "Failed to load run changelog",
      });
    }
  }, [appName, userId, sessionId, runKey]);

  // The eval report is the LAST artifact the run writes to GCS, so it can 404 if the
  // results page opens before the run's final write lands. fetchEvalReport retries on
  // 404 and reports "pending" so the UI can offer a refresh instead of silently
  // showing nothing (see lib/eval-report.ts).
  // `isCancelled` lets the load effect drop the result after unmount (the
  // refresh button passes nothing: it always applies).
  const loadEval = useCallback(async (sess: Session, isCancelled: () => boolean = () => false) => {
    const stateReport = parseStateEvalReport(sess.state?.creative_evaluation_report);
    const dir = outputDir(sess.state ?? {});
    if (!dir) {
      if (!isCancelled() && stateReport) {
        setEvalReport(stateReport);
        setEvalStatus("idle");
      }
      return;
    }

    setEvalStatus("loading");
    const variationsCount = countStateVariations(sess.state?.creative_variations);
    const baseEvalUrl = gcsProxyUrl(dir.bucket, `${dir.prefix}/creative_eval_report.json`);
    const evalUrl = variationsCount > 0 ? `${baseEvalUrl}&v=${variationsCount}` : baseEvalUrl;
    const result = await fetchEvalReport<unknown>(evalUrl);
    if (isCancelled()) return;
    if (result.status === "found") {
      // A malformed/older report (null) shows the "no evaluation" state unless
      // session state already carries a fresher valid report (e.g. post-variation).
      const gcsReport = parseEvalReport(result.report);
      if (gcsReport && stateReport) {
        const stateVisCount = stateReport.visual_concept_evaluations.length;
        const gcsVisCount = gcsReport.visual_concept_evaluations.length;
        setEvalReport(stateVisCount > gcsVisCount ? stateReport : gcsReport);
      } else {
        setEvalReport(gcsReport ?? stateReport);
      }
      setEvalStatus("idle");
    } else if (stateReport) {
      setEvalReport(stateReport);
      setEvalStatus("idle");
    } else {
      setEvalStatus(result.status);
    }
  }, []);

  useEffect(() => {
    if (!fromGe || !handoffToken) return;
    let cancelled = false;
    const controller = new AbortController();

    async function redeem() {
      try {
        const claimed = await claimRunHandoff(
          appName,
          userId,
          sessionId,
          handoffToken,
          { signal: controller.signal }
        );
        if (cancelled || controller.signal.aborted) return;
        router.replace(
          `/results/${encodeURIComponent(claimed.session_id)}?app=${encodeURIComponent(appName)}&from=ge`
        );
      } catch (err) {
        if (
          cancelled ||
          controller.signal.aborted ||
          (err instanceof Error && err.name === "AbortError")
        ) {
          return;
        }
        setError(
          err instanceof Error
            ? err.message
            : "Failed to claim Gemini Enterprise handoff"
        );
        setLoading(false);
      }
    }
    redeem();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [fromGe, handoffToken, appName, userId, sessionId, router]);

  useEffect(() => {
    if (fromGe && handoffToken) return;
    let cancelled = false;
    const isCancelled = () => cancelled;
    changelogAbortRef.current?.abort();
    changelogAbortRef.current = null;
    changelogFetchedKeyRef.current = null;

    async function load() {
      try {
        const [sess, artifactNames] = await Promise.all([
          getSession(appName, userId, sessionId),
          listArtifacts(appName, userId, sessionId),
        ]);
        if (cancelled) return;
        // Names only for general artifacts (image previews load from GCS). Video
        // payloads are fetched ONLY for cuts lacking a gs:// URI (e.g. local
        // memory-only ADK artifact mode).
        const videoUrls = await hydrateVideoArtifactUrls(
          appName,
          userId,
          sessionId,
          artifactNames,
          sess.state
        );
        if (cancelled) return;
        setSession(sess);
        setArtifacts(artifactNames.map((name) => ({ name })));
        setVideoArtifactUrls(videoUrls);
        setLoading(false);

        if (fromGe) {
          void loadChangelog();
        }

        // A run that stopped early never writes its eval report to GCS, so don't
        // poll for it (that would show "still being written" for a report that
        // won't come) — though if creative_evaluation_report is already in session
        // state (e.g. local memory:// dev mode without GCS, or a fail-soft GCS
        // save), hydrate it directly.
        if (sessionStoppedEarly(appName, sess.state ?? {}, sess.events ?? [])) {
          const fallbackReport = parseStateEvalReport(sess.state?.creative_evaluation_report);
          if (fallbackReport) {
            setEvalReport(fallbackReport);
            setEvalStatus("idle");
          }
          return;
        }
        await loadEval(sess, isCancelled);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof Error ? err.message : "Failed to load results"
        );
        setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
      changelogAbortRef.current?.abort();
    };
  }, [appName, userId, sessionId, fromGe, handoffToken, loadEval, loadChangelog, attempt]);

  const state = session?.state || EMPTY_STATE;

  const handleVariationsGenerated = useCallback(
    async (resp: GenerateVariationsResponse) => {
      setSession((prev) => (prev ? { ...prev, state: resp.state } : prev));
      const nextReport = parseStateEvalReport(resp.creative_evaluation_report);
      if (nextReport) {
        setEvalReport(nextReport);
        setEvalStatus("idle");
      }
      if (changelogFetchedKeyRef.current === runKey) {
        changelogFetchedKeyRef.current = null;
        void loadChangelog();
      }
      try {
        const artifactNames = await listArtifacts(appName, userId, sessionId);
        const videoUrls = await hydrateVideoArtifactUrls(
          appName,
          userId,
          sessionId,
          artifactNames,
          resp.state
        );
        setArtifacts(artifactNames.map((name) => ({ name })));
        setVideoArtifactUrls(videoUrls);
      } catch {
        // Non-fatal: contact sheet and GCS proxy URLs remain available.
      }
    },
    [appName, userId, sessionId, runKey, loadChangelog]
  );

  // Pair each visual concept (session state) with its ad copy and both evals.
  const proofs = useMemo(() => {
    const vcRaw = state.final_visual_concepts as { visual_concepts?: VisualConcept[] } | undefined;
    const acRaw = state.ad_copy_critique as { ad_copies?: AdCopy[] } | undefined;
    return buildProofs(
      vcRaw?.visual_concepts || [],
      acRaw?.ad_copies || [],
      evalReport,
      state.generated_images,
      state.person_reference_rejected,
      state.trend_situation,
      now,
      state.safer_angle_named_copies
    );
  }, [state, evalReport, now]);
  const sortedProofs = useMemo(() => sortProofs(proofs, sort), [proofs, sort]);
  // Failed blocking checks of the slate a share would publish (every rendered
  // proof; the api may still leave cast or expired ones out).
  const slateFailedChecks = useMemo(
    () => failedChecksFor(proofs.filter((p) => hasRenderedImage(state.generated_images, p.concept.concept_name))),
    [proofs, state.generated_images]
  );
  // Session state can be large; serialise it once per session, not on every render
  // (proof navigation, ratings, share-list updates all re-render this page).
  const stateJson = useMemo(() => JSON.stringify(state, null, 2), [state]);
  const ratingsEnabled =
    !loading &&
    !(fromGe && Boolean(handoffToken)) &&
    (appName === "creative_agent" || appName === "interactive_creative");
  const ratings = useSessionRatings(sessionId, ratingsEnabled);
  // People + variants for the proof dialog's Personalise panel: loaded once, on the
  // first open, and shared across creatives (the panel remounts per creative).
  const personalisation = usePersonalisationData(appName, sessionId, ratingsEnabled && detailOpen);
  const stopped = useMemo(
    () => (session ? sessionStoppedEarly(appName, state, session.events ?? []) : null),
    [appName, state, session]
  );

  if (loading) return <ResultsSkeleton />;

  if (error) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <ErrorState
          title="These results didn't load"
          message={
            <>
              <p className="text-mark-fail">{error}</p>
              <p className="mt-1">Check the session link, or open the run again from your run history.</p>
            </>
          }
          onRetry={() => {
            setError(null);
            setLoading(true);
            setAttempt((n) => n + 1);
          }}
          secondary={{ href: "/runs", label: "Open run history" }}
          className="p-6"
        />
      </div>
    );
  }

  const noImages = imagesRetryExhausted(state);
  // The eval report's run notes grouped into research gaps / failed saves /
  // quality flags (the image-exhaustion note is dropped: the zero-image banner
  // already covers it).
  const runWarnings = classifyWarnings(evalReport?.warnings);
  const gcsUri = [state.gcs_bucket, state.gcs_folder, state.agent_output_dir]
    .filter(Boolean)
    .join("/");

  const dir = outputDir(state);
  const outputUrl = (filename: string): string | null =>
    dir ? gcsProxyUrl(dir.bucket, `${dir.prefix}/${filename}`) : null;
  const galleryUrl = outputUrl("creative_portfolio_gallery.html");
  // No rendered images → no URLs (they'd only 404); tiles show "Image not rendered".
  const imagesMissing = imagesNotRendered(state);
  const imageUrlFor = (conceptName: string) =>
    imagesMissing ? null : outputUrl(conceptNameToFilename(conceptName));
  const runUrl = `/run/${sessionId}?${new URLSearchParams({ app: appName, userId }).toString()}`;

  const campaignFields = buildDisplayFields(state, CAMPAIGN_FIELD_DEFS);
  // Optional user visual art-direction inputs (PR #114) — hidden when unset
  const visualDirectionFields = buildDisplayFields(state, VISUAL_DIRECTION_FIELDS);

  const researchReport =
    typeof state.combined_final_cited_report === "string"
      ? state.combined_final_cited_report
      : "";

  const creativeBrief = parseCreativeBrief(state.creative_brief);

  // Does this run have the creative asset + eval view?
  const hasCreativeView =
    (appName === "creative_agent" || appName === "interactive_creative") && proofs.length > 0;
  const hasVideoReel = parseGeneratedVideos(state.generated_videos).length > 0;
  const canExportDeliveryKit =
    hasCreativeView || hasVideoReel || Boolean(researchReport) || Boolean(creativeBrief);
  const kitUrl = canExportDeliveryKit
    ? buildDeliveryKitUrl(appName, userId, sessionId)
    : null;
  const canGenerateVariations = hasCreativeView && !stopped;
  // Sharing freezes the rendered images into a public snapshot, so it needs them.
  const shareEnabled = hasCreativeView && !imagesMissing;
  const startedInGeminiEnterprise =
    fromGe ||
    state.run_origin === "gemini_enterprise" ||
    changelog?.origin === "gemini_enterprise";

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-6">
      <ResultsHeader
        appName={appName}
        sessionId={sessionId}
        summary={campaignSummary(campaignFields)}
        campaignFields={campaignFields}
        visualDirectionFields={visualDirectionFields}
        gcsUri={gcsUri}
        galleryUrl={galleryUrl}
        deliveryKitUrl={kitUrl}
        startedInGeminiEnterprise={startedInGeminiEnterprise}
        actions={
          shareEnabled && (
            <ShareDialog
              appName={appName}
              sessionId={sessionId}
              triggerLabel="Share slate"
              failedChecks={slateFailedChecks}
              onCreated={onShareCreated}
            />
          )
        }
      />

      <ResultsSummary
        report={evalReport}
        status={evalStatus}
        onRefresh={() => session && loadEval(session)}
        refreshDisabled={!session}
        noImages={noImages}
        runWarnings={runWarnings}
        stoppedBefore={stopped?.stage ?? null}
        runUrl={runUrl}
      />

      <LearningSummary state={state} className="mb-4" />
      <TrendSituationLine state={state} className="mb-4" />
      <SaferAngleLine state={state} className="mb-4" />

      {hasCreativeView && (
        <>
          <ProofGrid
            proofs={sortedProofs}
            sort={sort}
            onSortChange={setSort}
            imageUrlFor={imageUrlFor}
            onOpen={(i) => {
              setDetailIndex(i);
              setDetailOpen(true);
            }}
            isRated={(p) => isProofRated(p, ratings.byKey)}
            onOpenVariations={
              canGenerateVariations
                ? (p) => setVariationTarget(p)
                : undefined
            }
            itemRef={(i) => (el) => {
              if (el) proofButtons.current.set(i, el);
              else proofButtons.current.delete(i);
            }}
          />
          <ProofDetail
            proofs={sortedProofs}
            open={detailOpen}
            index={detailIndex}
            onIndexChange={setDetailIndex}
            onClose={() => setDetailOpen(false)}
            imageUrlFor={imageUrlFor}
            returnFocusTo={(i) => proofButtons.current.get(i) ?? null}
            onOpenVariations={
              canGenerateVariations
                ? (p) => {
                    setDetailOpen(false);
                    setVariationTarget(p);
                  }
                : undefined
            }
            ratingSlot={(p) => (
              <CreativeRatings
                proof={p}
                appName={appName}
                sessionId={sessionId}
                byKey={ratings.byKey}
                onChange={ratings.setRating}
              />
            )}
            shareSlot={(p) =>
              shareEnabled && hasRenderedImage(state.generated_images, p.concept.concept_name) ? (
                <ShareDialog
                  appName={appName}
                  sessionId={sessionId}
                  conceptNames={[p.concept.concept_name]}
                  triggerLabel="Share this creative"
                  failedChecks={failedChecksFor([p])}
                  onCreated={onShareCreated}
                />
              ) : null
            }
            personaliseSlot={(p) =>
              canPersonalise(p, state.generated_images, imagesMissing) ? (
                <PersonalisePanel
                  // Remount per creative so a preview never shows under another one.
                  key={p.concept.concept_name}
                  appName={appName}
                  sessionId={sessionId}
                  conceptName={p.concept.concept_name}
                  baseImageUrl={imageUrlFor(p.concept.concept_name)}
                  baseAlt={p.concept.concept_summary || p.concept.headline}
                  {...personalisationProps(personalisation, p.concept.concept_name)}
                />
              ) : null
            }
          />
          <VariationDrawer
            open={variationTarget !== null}
            proof={variationTarget}
            appName={appName}
            userId={userId}
            sessionId={sessionId}
            imageUrlFor={imageUrlFor}
            remainingSlots={Math.max(0, 16 - proofs.length)}
            onClose={() => setVariationTarget(null)}
            onGenerated={handleVariationsGenerated}
          />
        </>
      )}

      <VideoReelPanel
        state={state}
        artifactUrlByName={videoArtifactUrls}
        outputUrl={outputUrl}
      />

      {creativeBrief && (
        <QuietDisclosure title="Creative brief" className="mb-4">
          <CreativeBrief
            brief={creativeBrief}
            sources={state.sources as ReportSources | undefined}
            enterpriseSources={state.enterprise_sources as ReportSources | undefined}
            className="p-1"
          />
        </QuietDisclosure>
      )}

      {researchReport && (
        <QuietDisclosure title="Research report" className="mb-4">
          <ResearchReport
            markdown={researchReport}
            sources={state.sources as ReportSources | undefined}
            enterpriseSources={state.enterprise_sources as ReportSources | undefined}
            className="border-0 bg-transparent p-1"
          />
        </QuietDisclosure>
      )}

      {shareEnabled && <SessionShares sessionId={sessionId} created={newShares} />}

      {hasCreativeView && (
        <DeployDisclosure>
          <DeployPanel
            proofs={proofs}
            appName={appName}
            sessionId={sessionId}
            imageUrlFor={imageUrlFor}
            stoppedEarly={Boolean(stopped)}
            now={now}
          />
        </DeployDisclosure>
      )}

      <RunChangelogPanel
        changelog={changelog}
        loading={changelogLoading}
        error={changelogError}
        onOpen={loadChangelog}
        className="mb-4"
      />

      <ArtifactsPanel artifacts={artifacts} urlFor={outputUrl} />

      <QuietDisclosure title="Session state">
        <pre className="max-h-96 overflow-auto rounded-sm bg-muted/60 p-4 font-mono text-xs text-foreground/85">
          {stateJson}
        </pre>
      </QuietDisclosure>
    </div>
  );
}
