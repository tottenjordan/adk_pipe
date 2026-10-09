import { notFound } from "next/navigation";
import { cache } from "react";
import { ShareView } from "@/components/share/share-view";
import { loadSnapshot } from "@/lib/share-snapshot";
import { parseView } from "@/lib/share-views";

/** One storage read per request, shared by generateMetadata and the page. */
const getSnapshot = cache((token: string) => loadSnapshot(token));

/** Public share page: a frozen snapshot of one creative or a slate. */
export default async function SharePage({ params, searchParams }: PageProps<"/s/[token]">) {
  const { token } = await params;
  const snapshot = await getSnapshot(token);
  if (!snapshot) notFound();
  const { view } = await searchParams;

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 py-10 sm:px-6">
      <header>
        <h1 className="[font-stretch:75%] text-4xl font-extrabold leading-[1.05] tracking-tight text-foreground sm:text-5xl">
          {snapshot.brand} × {snapshot.trend}
        </h1>
        {snapshot.product && (
          <p className="mt-2 text-base text-muted-foreground">{snapshot.product}</p>
        )}
      </header>
      <ShareView snapshot={snapshot} initialView={parseView(view)} />
      <footer className="border-t border-border pt-4 text-sm text-muted-foreground">
        Shared from Trend Trawler · AI-generated images
      </footer>
    </div>
  );
}
