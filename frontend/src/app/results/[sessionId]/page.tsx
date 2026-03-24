"use client";

import { useEffect, useState, use } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
        setError(err instanceof Error ? err.message : "Failed to load results");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [appName, userId, sessionId]);

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12 text-center text-muted-foreground">
        Loading results...
      </div>
    );
  }

  if (error) {
    return (
      <div className="mx-auto max-w-4xl px-6 py-12">
        <Card className="border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive">{error}</p>
            <Link href="/">
              <Button variant="outline" className="mt-4">
                Back to Home
              </Button>
            </Link>
          </CardContent>
        </Card>
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
    return { bucket: bucketName, objectPath: `${folder}/${subdir}/creative_portfolio_gallery.html` };
  })();

  const imageArtifacts = artifacts.filter((a) => a.name.endsWith(".png") || a.name.endsWith(".jpg"));
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
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Results</h1>
          <p className="mt-1 text-sm text-muted-foreground font-mono">
            {appName} / {sessionId}
          </p>
        </div>
        <Link href="/">
          <Button variant="outline" size="sm">
            New Run
          </Button>
        </Link>
      </div>

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        {/* Left sidebar — GCS output + campaign metadata */}
        <div className="space-y-3">
          {gcsUri && <GcsWidget uri={gcsUri} />}

          <h2 className="text-sm font-semibold text-muted-foreground tracking-wider">
            Campaign Metadata
          </h2>
          {campaignFields.length === 0 ? (
            <p className="text-xs text-muted-foreground italic">
              No metadata available
            </p>
          ) : (
            campaignFields.map((f) => (
              <Card key={f.key} className="shadow-sm">
                <CardContent className="px-3 py-2.5">
                  <dt className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                    {f.label}
                  </dt>
                  <dd className="mt-0.5 text-sm font-medium leading-snug break-words">
                    {f.value}
                  </dd>
                </CardContent>
              </Card>
            ))
          )}
        </div>

        {/* Right content area */}
        <div>
          {/* Creative portfolio gallery — above artifacts */}
          {appName === "creative_agent" && galleryInfo && (
            <div className="mb-6">
              <GalleryViewer
                bucket={galleryInfo.bucket}
                objectPath={galleryInfo.objectPath}
              />
            </div>
          )}

          {/* Artifacts — collapsed by default */}
          {artifacts.length > 0 && (
            <Collapsible open={artifactsOpen} onOpenChange={setArtifactsOpen}>
              <Card className="mb-6">
                <CollapsibleTrigger className="w-full">
                  <CardHeader className="cursor-pointer hover:bg-muted/50 transition-colors">
                    <CardTitle className="text-base flex items-center justify-between">
                      <span>
                        Artifacts
                        <Badge variant="secondary" className="ml-2">
                          {artifacts.length}
                        </Badge>
                      </span>
                      <span className="text-xs text-muted-foreground font-normal">
                        {artifactsOpen ? "collapse" : "expand"}
                      </span>
                    </CardTitle>
                  </CardHeader>
                </CollapsibleTrigger>
                <CollapsibleContent>
                  <CardContent className="pt-0">
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
                            const folder = state.gcs_folder as string;
                            const subdir = state.agent_output_dir as string;
                            const src =
                              bucketName && folder && subdir
                                ? `/api/gcs?bucket=${encodeURIComponent(bucketName)}&path=${encodeURIComponent(`${folder}/${subdir}/${a.name}`)}`
                                : null;
                            return (
                              <div
                                key={a.name}
                                className="overflow-hidden rounded-lg border border-border"
                              >
                                {src ? (
                                  // eslint-disable-next-line @next/next/no-img-element
                                  <img
                                    src={src}
                                    alt={a.name}
                                    className="aspect-square w-full object-cover"
                                  />
                                ) : (
                                  <div className="flex aspect-square items-center justify-center bg-muted text-xs text-muted-foreground">
                                    No preview
                                  </div>
                                )}
                                <p className="truncate px-2 py-1.5 text-xs text-muted-foreground">
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
                              className="flex items-center justify-between rounded border border-border px-3 py-2"
                            >
                              <span className="text-sm font-mono">{a.name}</span>
                              <Badge variant="outline">PDF</Badge>
                            </li>
                          ))}
                        </ul>
                      </TabsContent>

                      <TabsContent value="html">
                        <ul className="space-y-2">
                          {htmlArtifacts.map((a) => (
                            <li
                              key={a.name}
                              className="flex items-center justify-between rounded border border-border px-3 py-2"
                            >
                              <span className="text-sm font-mono">{a.name}</span>
                              <Badge variant="outline">HTML</Badge>
                            </li>
                          ))}
                        </ul>
                      </TabsContent>

                      <TabsContent value="other">
                        <ul className="space-y-2">
                          {otherArtifacts.map((a) => (
                            <li
                              key={a.name}
                              className="rounded border border-border px-3 py-2 text-sm font-mono"
                            >
                              {a.name}
                            </li>
                          ))}
                        </ul>
                      </TabsContent>
                    </Tabs>
                  </CardContent>
                </CollapsibleContent>
              </Card>
            </Collapsible>
          )}

          {/* Raw session state */}
          <Collapsible open={stateOpen} onOpenChange={setStateOpen}>
            <Card>
              <CollapsibleTrigger className="w-full">
                <CardHeader className="cursor-pointer hover:bg-muted/50 transition-colors">
                  <CardTitle className="text-base flex items-center justify-between">
                    Session State
                    <span className="text-xs text-muted-foreground font-normal">
                      {stateOpen ? "collapse" : "expand"}
                    </span>
                  </CardTitle>
                </CardHeader>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <CardContent className="pt-0">
                  <pre className="max-h-96 overflow-auto rounded bg-muted p-4 text-xs font-mono">
                    {JSON.stringify(state, null, 2)}
                  </pre>
                </CardContent>
              </CollapsibleContent>
            </Card>
          </Collapsible>
        </div>
      </div>
    </div>
  );
}
