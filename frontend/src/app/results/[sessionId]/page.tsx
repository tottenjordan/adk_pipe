"use client";

import { useEffect, useState, useCallback, useMemo, useRef, use } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { buttonVariants } from "@/components/ui/button";
import { getSession, listArtifacts, getArtifact, SELF_USER_ID } from "@/lib/api";
import { fetchEvalReport } from "@/lib/eval-report";
import { gcsProxyUrl } from "@/lib/gcs";
import { classifyWarnings } from "@/lib/run-warnings";
import {
  buildDisplayFields,
  imagesRetryExhausted,
  VISUAL_DIRECTION_FIELDS,
  type DisplayFieldDef,
} from "@/lib/utils";
import type { Session } from "@/lib/types";
import {
  buildProofs,
  conceptNameToFilename,
  sortProofs,
  type AdCopy,
  type EvalReport,
  type ProofSort,
  type VisualConcept,
} from "@/lib/eval-matching";
import { campaignSummary } from "@/lib/results-copy";
import { imagesNotRendered, sessionStoppedEarly } from "@/lib/run-completion";
import { CreativeBrief } from "@/components/creative-brief";
import { ResearchReport } from "@/components/research-report";
import { parseCreativeBrief } from "@/lib/creative-brief";
import type { ReportSources } from "@/lib/research-report";
import { DeployPanel } from "./deploy-panel";
import { QuietDisclosure } from "@/components/quiet-disclosure";
import { ArtifactsPanel, type ArtifactData } from "./artifacts-panel";
import { ProofDetail } from "./proof-detail";
import { ProofGrid } from "./proof-grid";
import { CreativeRatings, useSessionRatings } from "./rating-control";
import { isProofRated } from "@/lib/ratings";
import { ResultsHeader } from "./results-header";
import { ResultsSummary, type EvalStatus } from "./results-summary";

const CAMPAIGN_FIELD_DEFS: DisplayFieldDef[] = [
  { label: "Brand", key: "brand" },
  { label: "Audience", key: "target_audience" },
  { label: "Product", key: "target_product" },
  { label: "Selling points", key: "key_selling_points" },
  { label: "Trend", key: "target_search_trends" },
];

const EMPTY_STATE: Record<string, unknown> = {};

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
  const [evalStatus, setEvalStatus] = useState<EvalStatus>("idle");
  const [sort, setSort] = useState<ProofSort>("pipeline");
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailIndex, setDetailIndex] = useState<number | null>(null);
  const proofButtons = useRef(new Map<number, HTMLButtonElement>());

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

        // A run that stopped early never writes its eval report, so don't poll
        // for it (that would show "still being written" for a report that won't
        // come). Otherwise fetch it, retrying on 404 for the last-written race.
        if (sessionStoppedEarly(appName, sess.state ?? {}, sess.events ?? [])) return;
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

  const state = session?.state || EMPTY_STATE;

  // Pair each visual concept (session state) with its ad copy and both evals.
  const proofs = useMemo(() => {
    const vcRaw = state.final_visual_concepts as { visual_concepts?: VisualConcept[] } | undefined;
    const acRaw = state.ad_copy_critique as { ad_copies?: AdCopy[] } | undefined;
    return buildProofs(
      vcRaw?.visual_concepts || [],
      acRaw?.ad_copies || [],
      evalReport,
      state.generated_images
    );
  }, [state, evalReport]);
  const sortedProofs = useMemo(() => sortProofs(proofs, sort), [proofs, sort]);
  const ratingsEnabled =
    !loading && (appName === "creative_agent" || appName === "interactive_creative");
  const ratings = useSessionRatings(sessionId, ratingsEnabled);
  const stopped = useMemo(
    () => (session ? sessionStoppedEarly(appName, state, session.events ?? []) : null),
    [appName, state, session]
  );

  if (loading) {
    return (
      <div className="flex flex-1 items-center justify-center py-24">
        <p className="text-sm text-muted-foreground" role="status">
          Loading results…
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <div className="rounded-lg border border-border bg-card p-6">
          <h1 className="text-lg font-semibold text-foreground">These results didn&apos;t load</h1>
          <p className="mt-1 text-sm text-mark-fail">{error}</p>
          <p className="mt-2 text-sm text-muted-foreground">
            Check the session link, or open the run again from your run history.
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Link href="/runs" className={buttonVariants({ variant: "outline" })}>
              Open run history
            </Link>
            <Link href="/" className={buttonVariants({ variant: "ghost" })}>
              Start a new run
            </Link>
          </div>
        </div>
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

  const bucketName =
    (state.gcs_bucket_name as string) ||
    (state.gcs_bucket as string)?.replace(/^gs:\/\//, "") ||
    "";
  const folder = state.gcs_folder as string;
  const subdir = state.agent_output_dir as string;
  const hasOutputDir = Boolean(bucketName && folder && subdir);

  const galleryUrl = hasOutputDir
    ? gcsProxyUrl(bucketName, `${folder}/${subdir}/creative_portfolio_gallery.html`)
    : null;

  const outputUrl = (filename: string): string | null =>
    hasOutputDir ? gcsProxyUrl(bucketName, `${folder}/${subdir}/${filename}`) : null;
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
            ratingSlot={(p) => (
              <CreativeRatings
                proof={p}
                appName={appName}
                sessionId={sessionId}
                byKey={ratings.byKey}
                onChange={ratings.setRating}
              />
            )}
          />
          <DeployPanel
            proofs={proofs}
            appName={appName}
            sessionId={sessionId}
            imageUrlFor={imageUrlFor}
            stoppedEarly={Boolean(stopped)}
          />
        </>
      )}

      {creativeBrief && (
        <QuietDisclosure title="Creative brief" className="mb-4">
          <CreativeBrief
            brief={creativeBrief}
            sources={state.sources as ReportSources | undefined}
            className="p-1"
          />
        </QuietDisclosure>
      )}

      {researchReport && (
        <QuietDisclosure title="Research report" className="mb-4">
          <ResearchReport
            markdown={researchReport}
            sources={state.sources as ReportSources | undefined}
            className="border-0 bg-transparent p-1"
          />
        </QuietDisclosure>
      )}

      <ArtifactsPanel artifacts={artifacts} urlFor={outputUrl} />

      <QuietDisclosure title="Session state">
        <pre className="max-h-96 overflow-auto rounded-sm bg-muted/60 p-4 font-mono text-xs text-foreground/85">
          {JSON.stringify(state, null, 2)}
        </pre>
      </QuietDisclosure>
    </div>
  );
}
