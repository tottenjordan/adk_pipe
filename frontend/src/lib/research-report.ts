/**
 * Research-report citation helpers (pure).
 *
 * The creative pipeline's `combined_final_cited_report` is markdown with inline
 * `<cite source="src-N" />` tags that point into the session-state `sources`
 * map. These helpers turn the tags into numbered citation links for display,
 * and into plain `[src-N]` markers for editing (converted back to canonical
 * tags before the edit is sent, since the backend re-renders citations from
 * the tag syntax).
 */

export interface ReportSource {
  title?: string;
  url?: string;
  domain?: string;
}

export type ReportSources = Record<string, ReportSource>;

export interface CitedSource {
  /** Display number, by first appearance in the report (1-based). */
  n: number;
  id: string;
  label: string;
  url?: string;
}

// Same shape the backend accepts (creative_agent/citations.py _CITE_TAG_RE).
const CITE_TAG = String.raw`<cite\s+source\s*=\s*["']?\s*(src-\d+)\s*["']?\s*/>`;
const citeTag = () => new RegExp(CITE_TAG, "g");
// Horizontal whitespace before a tag is dropped so the marker hugs the text.
const citeTagWithLeadingSpace = () => new RegExp(String.raw`[ \t]*` + CITE_TAG, "g");
// `[src-N]` edit marker — but not a markdown link labelled `src-N`.
const EDIT_MARKER = /\[(src-\d+)\](?!\()/g;

const escapeUrl = (url: string) =>
  url.replace(/ /g, "%20").replace(/\(/g, "%28").replace(/\)/g, "%29");

/**
 * Replace each cite tag with a numbered markdown link (`[1](url)`), numbering
 * sources by first appearance. Tags for unknown sources (or without a url)
 * are dropped, as the backend does. Returns the cited sources in number order.
 */
export function formatReportCitations(
  raw: string,
  sources: ReportSources | undefined
): { markdown: string; sources: CitedSource[] } {
  const cited: CitedSource[] = [];
  const byId = new Map<string, CitedSource>();
  const markdown = raw.replace(citeTagWithLeadingSpace(), (_match, id: string) => {
    let entry = byId.get(id);
    if (!entry) {
      const src = sources?.[id];
      if (!src?.url) return "";
      entry = {
        n: cited.length + 1,
        id,
        label: src.title || src.domain || id,
        url: src.url,
      };
      byId.set(id, entry);
      cited.push(entry);
    }
    return `[${entry.n}](${escapeUrl(entry.url!)})`;
  });
  return { markdown, sources: cited };
}

/** Normalise every cite tag to the canonical `<cite source="src-N"/>` form. */
export function canonicalizeCitations(raw: string): string {
  return raw.replace(citeTag(), (_m, id: string) => `<cite source="${id}"/>`);
}

/** Cite tags → `[src-N]` markers, for the edit textarea. */
export function toEditableReport(raw: string): string {
  return raw.replace(citeTag(), (_m, id: string) => `[${id}]`);
}

/** `[src-N]` markers → canonical `<cite source="src-N"/>` tags. */
export function fromEditableReport(text: string): string {
  return text.replace(EDIT_MARKER, (_m, id: string) => `<cite source="${id}"/>`);
}
