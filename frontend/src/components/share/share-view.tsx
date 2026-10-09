"use client";

import { useState, type ComponentType } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import type { ShareSnapshotV1 } from "@/lib/share-snapshot";
import { parseView, SHARE_VIEWS, type ShareViewName } from "@/lib/share-views";
import { FeedPost } from "./feed-post";
import { ShareCard } from "./share-card";
import { ShareEval } from "./share-eval";
import type { ShareCreativeProps } from "./share-image";
import { StoryFrame } from "./story-frame";

const VIEW_COMPONENTS: Record<ShareViewName, ComponentType<ShareCreativeProps>> = {
  card: ShareCard,
  feed: FeedPost,
  story: StoryFrame,
};

const LIST_LAYOUT: Record<ShareViewName, string> = {
  card: "space-y-8",
  feed: "grid gap-8 md:grid-cols-2",
  story: "grid gap-8 sm:grid-cols-2 lg:grid-cols-4",
};

function Creatives({ snapshot, view }: { snapshot: ShareSnapshotV1; view: ShareViewName }) {
  const View = VIEW_COMPONENTS[view];
  const articles = snapshot.creatives.map((creative) => {
    const headingId = `share-${view}-${creative.index}-title`;
    return (
      <article key={creative.index} aria-labelledby={headingId}>
        <View token={snapshot.token} brand={snapshot.brand} creative={creative} headingId={headingId} />
        {snapshot.include_eval && creative.eval && <ShareEval data={creative.eval} />}
      </article>
    );
  });
  if (snapshot.scope === "creative" || articles.length === 1) {
    return <div className={cn(view !== "card" && "max-w-[470px]")}>{articles[0]}</div>;
  }
  return (
    <ul className={LIST_LAYOUT[view]}>
      {articles.map((a) => (
        <li key={a.key}>{a}</li>
      ))}
    </ul>
  );
}

/** Card / feed / story switcher; the chosen view is kept in `?view=` (shareable). */
export function ShareView({
  snapshot,
  initialView,
}: {
  snapshot: ShareSnapshotV1;
  initialView: ShareViewName;
}) {
  const [view, setView] = useState<ShareViewName>(initialView);

  const onChange = (value: unknown) => {
    const next = parseView(typeof value === "string" ? value : undefined);
    setView(next);
    const url = new URL(window.location.href);
    url.searchParams.set("view", next);
    window.history.replaceState(null, "", url.toString());
  };

  return (
    <Tabs value={view} onValueChange={onChange} className="gap-6">
      <TabsList aria-label="Presentation" activateOnFocus>
        {SHARE_VIEWS.map((v) => (
          <TabsTrigger key={v.value} value={v.value} className="px-4">
            {v.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {SHARE_VIEWS.map((v) => (
        <TabsContent key={v.value} value={v.value} className="text-base">
          <Creatives snapshot={snapshot} view={v.value} />
        </TabsContent>
      ))}
    </Tabs>
  );
}
