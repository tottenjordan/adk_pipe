/**
 * Research-report citation helpers (pure).
 *
 * The creative pipeline's `combined_final_cited_report` is markdown with inline
 * `<cite source="src-N" />` and `<cite source="ent-N" />` tags that point into
 * the session-state `sources` and `enterprise_sources` maps. These helpers turn
 * the tags into numbered citation links or inline enterprise badges for display,
 * and into plain `[src-N]` / `[ent-N]` markers for editing (converted back to
 * canonical tags before the edit is sent, since the backend re-renders
 * citations from the tag syntax).
 */

export interface ReportSource {
  title?: string;
  url?: string;
  domain?: string;
  source_type?: string;
}

export type ReportSources = Record<string, ReportSource>;

export interface CitedSource {
  /** Display number, by first appearance in the report (1-based). */
  n: number;
  id: string;
  label: string;
  url?: string;
  isEnterprise?: boolean;
}

// Same shape the backend accepts (creative_agent/citations.py _CITE_TAG_RE).
const CITE_TAG = String.raw`<cite\s+source\s*=\s*(?:["']([^"']*)["']|((?:src|ent)-\d+))\s*/>`;
const citeTag = () => new RegExp(CITE_TAG, "g");
// Horizontal whitespace before a tag is dropped so the marker hugs the text.
const citeTagWithLeadingSpace = () => new RegExp(String.raw`[ \t]*` + CITE_TAG, "g");
// `[src-N]` or `[ent-N]` edit marker — but not a markdown link labelled `src-N`.
const EDIT_MARKER = /\[((?:src|ent)-\d+)\](?!\()/g;
const INLINE_ENT_MARKER = /\[(ent-\d+)\](?!\()/g;
const INLINE_ENTERPRISE_LABEL = /\[Enterprise:\s*([^\]]+)\](?!\()/g;

const escapeUrl = (url: string) =>
  url.replace(/ /g, "%20").replace(/\(/g, "%28").replace(/\)/g, "%29");

const isHttpUrl = (url: string | undefined): url is string =>
  Boolean(url && /^https?:\/\//i.test(url));

/** Strip `gs://` schemes from enterprise fallback labels so raw GCS URIs never surface in UI badges. */
export function cleanEnterpriseLabel(raw: string | undefined): string {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return "";
  if (/^gs:\/\//i.test(trimmed)) {
    const stripped = trimmed.slice(5).replace(/\/+$/, "");
    if (!stripped) return "";
    const parts = stripped.split("/");
    return parts[parts.length - 1] || stripped;
  }
  return trimmed;
}

const parseTagIds = (quoted?: string, bare?: string): string[] =>
  (quoted ?? bare ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => /^(?:src|ent)-\d+$/.test(s));

/**
 * Replace each cite tag with a numbered markdown link (`[1](url)`) or an
 * internal enterprise badge marker (`[Enterprise](#enterprise-cite-N)`),
 * numbering sources by first appearance. Tags for unknown sources (or web
 * sources without an http(s) url) are dropped, as the backend does. Returns the
 * cited sources in number order.
 */
export function formatReportCitations(
  raw: string,
  sources: ReportSources | undefined,
  enterpriseSources?: ReportSources
): { markdown: string; sources: CitedSource[] } {
  const allSources: ReportSources | undefined =
    sources || enterpriseSources ? { ...sources, ...enterpriseSources } : undefined;
  const cited: CitedSource[] = [];
  const byId = new Map<string, CitedSource>();

  const resolveEntry = (id: string): CitedSource | null => {
    const existing = byId.get(id);
    if (existing) return existing;
    const src = allSources?.[id];
    if (!src) return null;
    const isEnterprise = id.startsWith("ent-") || src.source_type === "enterprise";
    if (!isEnterprise) {
      if (!isHttpUrl(src.url)) return null;
      const entry: CitedSource = {
        n: cited.length + 1,
        id,
        label: src.title || src.domain || id,
        url: src.url,
      };
      byId.set(id, entry);
      cited.push(entry);
      return entry;
    }
    const httpUrl = isHttpUrl(src.url) ? src.url : undefined;
    const label =
      cleanEnterpriseLabel(src.title) || cleanEnterpriseLabel(src.domain) || id;
    const entry: CitedSource = {
      n: cited.length + 1,
      id,
      label,
      ...(httpUrl ? { url: httpUrl } : {}),
      isEnterprise: true,
    };
    byId.set(id, entry);
    cited.push(entry);
    return entry;
  };

  let markdown = raw.replace(
    citeTagWithLeadingSpace(),
    (match, quoted?: string, bare?: string) => {
      const ids = parseTagIds(quoted, bare);
      if (!ids.length) return "";
      const hasLeadingSpace = /^[ \t]/.test(match);
      const tokens: string[] = [];
      for (const id of ids) {
        const entry = resolveEntry(id);
        if (!entry) continue;
        if (entry.url) {
          tokens.push(`[${entry.n}](${escapeUrl(entry.url)})`);
        } else {
          const prefix = tokens.length === 0 && hasLeadingSpace ? " " : tokens.length > 0 ? " " : "";
          tokens.push(`${prefix}[Enterprise](#enterprise-cite-${entry.n})`);
        }
      }
      return tokens.join("");
    }
  );

  markdown = markdown.replace(INLINE_ENT_MARKER, (_match, id: string) => {
    const entry = resolveEntry(id);
    if (!entry) return "";
    if (entry.url) return `[${entry.n}](${escapeUrl(entry.url)})`;
    return `[Enterprise](#enterprise-cite-${entry.n})`;
  });

  markdown = markdown.replace(INLINE_ENTERPRISE_LABEL, (_match, label: string) => {
    const safeLabel = label
      .replace(/[\r\n]+/g, " ")
      .replace(/[[\]()]/g, "")
      .trim();
    return safeLabel
      ? `[Enterprise: ${safeLabel}](#enterprise-inline)`
      : `[Enterprise](#enterprise-inline)`;
  });

  return { markdown, sources: cited };
}

/** Normalise every cite tag to canonical `<cite source="..."/>` tags. */
export function canonicalizeCitations(raw: string): string {
  return raw.replace(citeTag(), (match, quoted?: string, bare?: string) => {
    const ids = parseTagIds(quoted, bare);
    return ids.length ? ids.map((id) => `<cite source="${id}"/>`).join("") : match;
  });
}

/** Cite tags → `[src-N]` / `[ent-N]` markers, for the edit textarea. */
export function toEditableReport(raw: string): string {
  return raw.replace(citeTag(), (match, quoted?: string, bare?: string) => {
    const ids = parseTagIds(quoted, bare);
    return ids.length ? ids.map((id) => `[${id}]`).join("") : match;
  });
}

/** `[src-N]` / `[ent-N]` markers → canonical `<cite source="..."/>` tags. */
export function fromEditableReport(text: string): string {
  return text.replace(EDIT_MARKER, (_m, id: string) => `<cite source="${id}"/>`);
}
