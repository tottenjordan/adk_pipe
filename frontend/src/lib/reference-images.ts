import type { CampaignInput, ReferenceImageInput } from "@/lib/types";

/**
 * Reference images for image generation (creative agents). Mirrors the
 * backend's `creative_agent/references.py`: up to {@link MAX_REFERENCE_IMAGES}
 * `{uri, role}` entries, the legacy `reference_image_uri`/`reference_image_role`
 * pair first (an empty role there means `product`), deduped by uri. A
 * `reference_images` entry must name a valid role or it is skipped.
 */
export const MAX_REFERENCE_IMAGES = 3;

export const REFERENCE_ROLES = ["product", "logo", "style"] as const;

const DEFAULT_ROLE = "product";

/** A `gs://bucket/object` or `http(s)://…` URI (the forms the backend fetches). */
const REFERENCE_URI_RE = /^(gs:\/\/[^/\s]+\/\S+|https?:\/\/\S+)$/;

export function isReferenceUri(uri: string): boolean {
  return REFERENCE_URI_RE.test(uri.trim());
}

type ReferenceFormFields = Pick<
  CampaignInput,
  "referenceImageUri" | "referenceImageRole" | "extraReferenceImages"
>;

/** The form's reference rows, row 1 (the legacy field pair) first. */
function formRows(form: ReferenceFormFields): ReferenceImageInput[] {
  return [
    { uri: form.referenceImageUri ?? "", role: form.referenceImageRole ?? "" },
    ...(form.extraReferenceImages ?? []),
  ];
}

/** Non-blank form rows whose URI is not gs:// or http(s) (these block submit). */
export function invalidReferenceUris(form: ReferenceFormFields): string[] {
  return formRows(form)
    .map((r) => r.uri.trim())
    .filter((uri) => uri && !isReferenceUri(uri));
}

function normaliseRole(role: unknown, fallback: string | null): string | null {
  if (role == null) return fallback;
  if (typeof role !== "string") return null;
  const r = role.trim().toLowerCase();
  if (!r) return fallback;
  return (REFERENCE_ROLES as readonly string[]).includes(r) ? r : null;
}

/**
 * Ordered, valid, deduped, capped references from raw `{uri, role}`
 * candidates; `fallback` is the role a candidate with no/empty role gets
 * (null = skip it).
 */
function collect(candidates: Array<[unknown, string | null]>): ReferenceImageInput[] {
  const refs: ReferenceImageInput[] = [];
  const seen = new Set<string>();
  for (const [entry, fallback] of candidates) {
    if (refs.length === MAX_REFERENCE_IMAGES) break;
    if (!entry || typeof entry !== "object") continue;
    const { uri: rawUri, role: rawRole } = entry as Record<string, unknown>;
    if (typeof rawUri !== "string" || !rawUri.trim()) continue;
    const uri = rawUri.trim();
    const role = normaliseRole(rawRole, fallback);
    if (role === null) {
      console.warn(`Skipping reference image ${uri}: invalid role ${JSON.stringify(rawRole)}`);
      continue;
    }
    if (seen.has(uri)) continue;
    seen.add(uri);
    refs.push({ uri, role });
  }
  return refs;
}

/**
 * The form's reference rows (row 1 = the legacy field pair) → state entries.
 * The form's empty role means product, so every emitted entry names its role;
 * rows with an invalid URI are never emitted.
 */
export function referenceImagesFromForm(form: ReferenceFormFields): ReferenceImageInput[] {
  return collect(
    formRows(form)
      .filter((r) => isReferenceUri(r.uri))
      .map((r) => [r, DEFAULT_ROLE]),
  );
}

function listedReferences(raw: unknown): unknown[] {
  if (typeof raw === "string") {
    try {
      raw = raw.trim() ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  }
  return Array.isArray(raw) ? raw : [];
}

/** A session's references: legacy pair first, then `reference_images`. */
export function resolveReferenceImages(state: Record<string, unknown>): ReferenceImageInput[] {
  return collect([
    [{ uri: state.reference_image_uri, role: state.reference_image_role }, DEFAULT_ROLE],
    ...listedReferences(state.reference_images).map((e): [unknown, null] => [e, null]),
  ]);
}

/** Read-only display: "product: gs://…; style: https://…" ("" when none). */
export function formatReferenceImages(state: Record<string, unknown>): string {
  return resolveReferenceImages(state)
    .map((r) => `${r.role}: ${r.uri}`)
    .join("; ");
}

/**
 * A session's references split into form rows for "Duplicate brief": row 1
 * (the legacy pair, or the first listed reference when that pair is empty)
 * and the extra rows after it.
 */
export function referenceRowsFromState(state: Record<string, unknown>): {
  first: ReferenceImageInput | null;
  extras: ReferenceImageInput[];
} {
  const [first = null, ...extras] = resolveReferenceImages(state);
  return { first, extras };
}
