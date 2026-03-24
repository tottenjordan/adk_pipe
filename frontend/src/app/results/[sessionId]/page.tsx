"use client";

import { useEffect, useState, use } from "react";
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
import { GalleryViewer } from "@/components/gallery-viewer";
import { getSession, listArtifacts, getArtifact } from "@/lib/api";
import type { Session } from "@/lib/types";

interface ArtifactData {
  name: string;
  data: unknown;
}

export default function ResultsPage({
  params,
}: {
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = use(params);
  const searchParams = useSearchParams();
  const appName = searchParams.get("app") || "trend_trawler";
  const userId = searchParams.get("userId") || "default_user";

  const [session, setSession] = useState<Session | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stateOpen, setStateOpen] = useState(false);
  const [artifactsOpen, setArtifactsOpen] = useState(false);

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
      } catch (err) {
        setError(
          err instanceof Error ? err.message : "Failed to load results"
        );
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [appName, userId, sessionId]);

  if (loading) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center py-24">
        <div className="flex space-x-1.5 mb-4">
          <div className="h-2.5 w-2.5 rounded-full bg-blue-400 animate-bounce" style={{ animationDelay: "0ms" }} />
          <div className="h-2.5 w-2.5 rounded-full bg-purple-400 animate-bounce" style={{ animationDelay: "150ms" }} />
          <div className="h-2.5 w-2.5 rounded-full bg-emerald-400 animate-bounce" style={{ animationDelay: "300ms" }} />
        </div>
        <p className="text-sm text-muted-foreground">Loading results...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <div className="glass rounded-2xl p-6">
          <p className="text-red-400">{error}</p>
          <Link href="/">
            <Button variant="outline" className="mt-4 border-white/10 bg-white/5 hover:bg-white/10">
              Back to Home
            </Button>
          </Link>
        </div>
      </div>
    );
  }

  const state = session?.state || {};
  const gcsUri = [state.gcs_bucket, state.gcs_folder, state.agent_output_dir]
    .filter(Boolean)
    .join("/");

  const bucketName =
    (state.gcs_bucket_name as string) ||
    (state.gcs_bucket as string)?.replace(/^gs:\/\//, "") ||
    "";

  const galleryInfo = (() => {
    const folder = state.gcs_folder as string;
    const subdir = state.agent_output_dir as string;
    if (!bucketName || !folder || !subdir) return null;
    return {
      bucket: bucketName,
      objectPath: `${folder}/${subdir}/creative_portfolio_gallery.html`,
    };
  })();

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

  // Campaign metadata fields for left sidebar
  const campaignFields = [
    { label: "Brand", key: "brand" },
    { label: "Target Audience", key: "target_audience" },
    { label: "Target Product", key: "target_product" },
    { label: "Key Selling Points", key: "key_selling_points" },
    { label: "Search Trend", key: "target_search_trends" },
  ]
    .map((f) => ({ ...f, value: state[f.key] as string | undefined }))
    .filter((f) => f.value);

  return (
    <div className="mx-auto max-w-[1600px] px-6 py-8">
      <div className="mb-6 flex items-center justify-between animate-fadeIn">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            Results
          </h1>
          <p className="mt-1 text-sm text-muted-foreground font-mono">
            {appName} / {sessionId}
          </p>
        </div>
        <Link href="/">
          <Button variant="outline" size="sm" className="border-white/10 bg-white/5 hover:bg-white/10">
            New Run
          </Button>
        </Link>
      </div>

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        {/* Left sidebar */}
        <div className="space-y-3 animate-fadeInUp animation-delay-100 opacity-0" style={{ animationFillMode: "forwards" }}>
          {gcsUri && <GcsWidget uri={gcsUri} />}

          <h2 className="text-[10px] font-semibold text-muted-foreground tracking-wider uppercase pt-1">
            Campaign Metadata
          </h2>
          {campaignFields.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No metadata available
            </p>
          ) : (
            campaignFields.map((f) => (
              <div key={f.key} className="glass rounded-xl px-4 py-3">
                <dt className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                  {f.label}
                </dt>
                <dd className="mt-0.5 text-sm font-medium leading-snug break-words text-foreground">
                  {f.value}
                </dd>
              </div>
            ))
          )}
        </div>

        {/* Right content area */}
        <div className="animate-fadeInUp animation-delay-200 opacity-0" style={{ animationFillMode: "forwards" }}>
          {/* Creative portfolio gallery */}
          {appName === "creative_agent" && galleryInfo && (
            <div className="mb-6">
              <GalleryViewer
                bucket={galleryInfo.bucket}
                objectPath={galleryInfo.objectPath}
              />
            </div>
          )}

          {/* Artifacts */}
          {artifacts.length > 0 && (
            <Collapsible open={artifactsOpen} onOpenChange={setArtifactsOpen}>
              <div className="glass rounded-2xl mb-6 overflow-hidden">
                <CollapsibleTrigger className="w-full">
                  <div className="cursor-pointer hover:bg-white/3 transition-colors px-5 py-3 flex items-center justify-between">
                    <span className="text-sm font-semibold text-foreground flex items-center gap-2">
                      Artifacts
                      <Badge
                        variant="secondary"
                        className="bg-primary/15 text-primary border-0 text-[10px]"
                      >
                        {artifacts.length}
                      </Badge>
                    </span>
                    <span className="text-[10px] text-muted-foreground">
                      {artifactsOpen ? "collapse" : "expand"}
                    </span>
                  </div>
                </CollapsibleTrigger>
                <CollapsibleContent>
                  <div className="px-5 pb-5">
                    <Tabs defaultValue="images">
                      <TabsList className="bg-white/5 border border-white/5">
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
                            const folder = state.gcs_folder as string;
                            const subdir = state.agent_output_dir as string;
                            const src =
                              bucketName && folder && subdir
                                ? `/api/gcs?bucket=${encodeURIComponent(bucketName)}&path=${encodeURIComponent(`${folder}/${subdir}/${a.name}`)}`
                                : null;
                            return (
                              <div
                                key={a.name}
                                className="overflow-hidden rounded-xl glass transition-all hover:shadow-lg hover:shadow-primary/5"
                              >
                                {src ? (
                                  // eslint-disable-next-line @next/next/no-img-element
                                  <img
                                    src={src}
                                    alt={a.name}
                                    className="aspect-square w-full object-cover"
                                  />
                                ) : (
                                  <div className="flex aspect-square items-center justify-center bg-white/5 text-xs text-muted-foreground">
                                    No preview
                                  </div>
                                )}
                                <p className="truncate px-3 py-2 text-[10px] text-muted-foreground">
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
                              className="flex items-center justify-between rounded-lg glass px-4 py-3"
                            >
                              <span className="text-sm font-mono text-foreground/80">
                                {a.name}
                              </span>
                              <Badge
                                variant="outline"
                                className="border-white/10"
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
                              className="flex items-center justify-between rounded-lg glass px-4 py-3"
                            >
                              <span className="text-sm font-mono text-foreground/80">
                                {a.name}
                              </span>
                              <Badge
                                variant="outline"
                                className="border-white/10"
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
                              className="rounded-lg glass px-4 py-3 text-sm font-mono text-foreground/80"
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
            <div className="glass rounded-2xl overflow-hidden">
              <CollapsibleTrigger className="w-full">
                <div className="cursor-pointer hover:bg-white/3 transition-colors px-5 py-3 flex items-center justify-between">
                  <span className="text-sm font-semibold text-foreground">
                    Session State
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {stateOpen ? "collapse" : "expand"}
                  </span>
                </div>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="px-5 pb-5">
                  <pre className="max-h-96 overflow-auto rounded-lg bg-white/5 p-4 text-xs font-mono text-foreground/70">
                    {JSON.stringify(state, null, 2)}
                  </pre>
                </div>
              </CollapsibleContent>
            </div>
          </Collapsible>
        </div>
      </div>
    </div>
  );
}
