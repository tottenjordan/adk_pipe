"use client";

import { QuietDisclosure } from "@/components/quiet-disclosure";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface ArtifactData {
  name: string;
  data: unknown;
}

function NameList({ items, kind }: { items: ArtifactData[]; kind?: string }) {
  return (
    <ul className="divide-y divide-border rounded-sm border border-border">
      {items.map((a) => (
        <li key={a.name} className="flex items-center justify-between gap-3 px-3 py-2">
          <span className="truncate font-mono text-xs text-foreground/85">{a.name}</span>
          {kind && <span className="shrink-0 text-xs text-muted-foreground">{kind}</span>}
        </li>
      ))}
    </ul>
  );
}

/** Saved ADK artifacts grouped by type in tabs (images, PDFs, HTML, other). */
export function ArtifactsPanel({
  artifacts,
  urlFor,
}: {
  artifacts: ArtifactData[];
  /** GCS proxy URL for an artifact name, or null when the run has no output folder. */
  urlFor: (name: string) => string | null;
}) {
  if (artifacts.length === 0) return null;

  const isImage = (n: string) => n.endsWith(".png") || n.endsWith(".jpg");
  const images = artifacts.filter((a) => isImage(a.name));
  const pdfs = artifacts.filter((a) => a.name.endsWith(".pdf"));
  const html = artifacts.filter((a) => a.name.endsWith(".html"));
  const other = artifacts.filter(
    (a) => !isImage(a.name) && !a.name.endsWith(".pdf") && !a.name.endsWith(".html")
  );
  const groups = [
    { value: "images", label: "Images", items: images },
    { value: "pdfs", label: "PDFs", items: pdfs },
    { value: "html", label: "HTML", items: html },
    { value: "other", label: "Other", items: other },
  ].filter((g) => g.items.length > 0);

  return (
    <QuietDisclosure title="Artifacts" count={artifacts.length} className="mb-4">
      <Tabs defaultValue={groups[0]?.value}>
        <TabsList>
          {groups.map((g) => (
            <TabsTrigger key={g.value} value={g.value}>
              {g.label} <span className="tabular-nums">{g.items.length}</span>
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="images">
          <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            {images.map((a) => {
              const src = urlFor(a.name);
              return (
                <li key={a.name} className="rounded-sm border border-border p-1.5">
                  {src ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={src} alt={a.name} className="aspect-square w-full rounded-none object-cover" />
                  ) : (
                    <div className="flex aspect-square items-center justify-center bg-muted text-xs text-muted-foreground">
                      No preview
                    </div>
                  )}
                  <p className="truncate pt-1.5 font-mono text-xs text-muted-foreground">{a.name}</p>
                </li>
              );
            })}
          </ul>
        </TabsContent>
        <TabsContent value="pdfs">
          <NameList items={pdfs} kind="PDF" />
        </TabsContent>
        <TabsContent value="html">
          <NameList items={html} kind="HTML" />
        </TabsContent>
        <TabsContent value="other">
          <NameList items={other} />
        </TabsContent>
      </Tabs>
    </QuietDisclosure>
  );
}
