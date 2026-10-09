import type { Metadata } from "next";
import type { ShareSnapshotV1 } from "@/lib/share-snapshot";
import { imageAlt, shareImageSrc } from "@/lib/share-views";

const DESCRIPTION_MAX = 160;
const NOINDEX = { index: false, follow: false } as const;

/** `SHARE_BASE_URL` (the share service's public origin) or null when unset/invalid. */
export function shareBaseUrl(env: Record<string, string | undefined> = process.env): string | null {
  const raw = (env.SHARE_BASE_URL ?? "").trim().replace(/\/+$/, "");
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" || url.protocol === "http:" ? raw : null;
  } catch {
    return null;
  }
}

function clip(text: string, max: number): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

/** Link-preview metadata (Open Graph + Twitter) for a share; never indexed. */
export function buildShareMetadata(snapshot: ShareSnapshotV1, baseUrl: string | null): Metadata {
  const first = snapshot.creatives[0];
  const pageTitle = `${snapshot.brand} × ${snapshot.trend}`;
  const n = snapshot.creatives.length;
  const title =
    snapshot.scope === "creative" || n === 1
      ? first.headline || pageTitle
      : `${pageTitle} — ${n} creatives`;
  const description = clip(first.caption || first.body || snapshot.product, DESCRIPTION_MAX);
  const imageUrl = baseUrl ? `${baseUrl}${shareImageSrc(snapshot.token, first.index)}` : null;
  const alt = imageAlt(first.alt, first.headline);

  return {
    title: pageTitle,
    description,
    robots: NOINDEX,
    ...(baseUrl ? { metadataBase: new URL(baseUrl) } : {}),
    openGraph: {
      type: "website",
      title,
      description,
      ...(imageUrl ? { images: [{ url: imageUrl, alt }] } : {}),
    },
    twitter: {
      card: "summary_large_image",
      title,
      description,
      ...(imageUrl ? { images: [imageUrl] } : {}),
    },
  };
}

/** Metadata for a missing, malformed or revoked link. */
export const MISSING_SHARE_METADATA: Metadata = {
  title: "Link unavailable",
  robots: NOINDEX,
};
