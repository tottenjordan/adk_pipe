/** Share page presentations (shared by the server page and the client switcher). */

export const SHARE_VIEWS = [
  { value: "card", label: "Card" },
  { value: "feed", label: "Feed" },
  { value: "story", label: "Story" },
] as const;

export type ShareViewName = (typeof SHARE_VIEWS)[number]["value"];

/** `?view=` → a known view (first value wins), else the card. */
export function parseView(raw: string | string[] | undefined): ShareViewName {
  const value = Array.isArray(raw) ? raw[0] : raw;
  return SHARE_VIEWS.find((v) => v.value === value)?.value ?? "card";
}

/** Same-origin image URL for creative `index` of share `token`. */
export function shareImageSrc(token: string, index: number): string {
  return `/s/${token}/img/${index}`;
}

/** Validated `"4:5"` → CSS `aspect-ratio` (`"4 / 5"`); unknown → 4:5. */
export function cssAspectRatio(ratio: string): string {
  const m = /^([1-9][0-9]?):([1-9][0-9]?)$/.exec(ratio);
  return m ? `${m[1]} / ${m[2]}` : "4 / 5";
}

/** Alt text: the snapshot's alt, else the headline, else a generic label. */
export function imageAlt(alt: string, headline: string): string {
  return alt.trim() || headline.trim() || "Ad creative";
}

export const CAPTION_PREVIEW_CHARS = 125;

/** Feed caption preview: cut on a word boundary when longer than the preview length. */
export function captionPreview(caption: string): { text: string; truncated: boolean } {
  if (caption.length <= CAPTION_PREVIEW_CHARS) return { text: caption, truncated: false };
  const cut = caption.slice(0, CAPTION_PREVIEW_CHARS);
  const space = cut.lastIndexOf(" ");
  return { text: (space > 60 ? cut.slice(0, space) : cut).trimEnd(), truncated: true };
}
