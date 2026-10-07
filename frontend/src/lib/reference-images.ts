import type { CampaignInput, ReferenceImageInput } from "@/lib/types";

/**
 * Reference images for image generation (creative agents). Mirrors the
 * backend's `creative_agent/references.py`: up to {@link MAX_REFERENCE_IMAGES}
 * `{uri, role}` entries, the legacy `reference_image_uri`/`reference_image_role`
 * pair first, deduped by uri, an empty role meaning `product`.
 */
export const MAX_REFERENCE_IMAGES = 3;

export const REFERENCE_ROLES = ["product", "logo", "style"] as const;

const DEFAULT_ROLE = "product";

function normaliseRole(role: unknown): string | null {
  if (role == null) return DEFAULT_ROLE;
  if (typeof role !== "string") return null;
  const r = role.trim().toLowerCase() || DEFAULT_ROLE;
  return (REFERENCE_ROLES as readonly string[]).includes(r) ? r : null;
}

/** Ordered, valid, deduped, capped references from raw `{uri, role}` candidates. */
function collect(candidates: unknown[]): ReferenceImageInput[] {
  const refs: ReferenceImageInput[] = [];
  const seen = new Set<string>();
  for (const entry of candidates) {
    if (refs.length === MAX_REFERENCE_IMAGES) break;
    if (!entry || typeof entry !== "object") continue;
    const { uri: rawUri, role: rawRole } = entry as Record<string, unknown>;
    if (typeof rawUri !== "string" || !rawUri.trim()) continue;
    const uri = rawUri.trim();
    const role = normaliseRole(rawRole);
    if (role === null || seen.has(uri)) continue;
    seen.add(uri);
    refs.push({ uri, role });
  }
  return refs;
}

/** The form's reference rows (row 1 = the legacy field pair) → state entries. */
export function referenceImagesFromForm(
  form: Pick<CampaignInput, "referenceImageUri" | "referenceImageRole" | "extraReferenceImages">,
): ReferenceImageInput[] {
  return collect([
    { uri: form.referenceImageUri, role: form.referenceImageRole || "" },
    ...(form.extraReferenceImages ?? []),
  ]);
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
    { uri: state.reference_image_uri, role: state.reference_image_role },
    ...listedReferences(state.reference_images),
  ]);
}

/** Read-only display: "product: gs://…; style: https://…" ("" when none). */
export function formatReferenceImages(state: Record<string, unknown>): string {
  return resolveReferenceImages(state)
    .map((r) => `${r.role}: ${r.uri}`)
    .join("; ");
}

/** The references after row 1 (the legacy pair), for "Duplicate brief". */
export function extraReferencesFromState(state: Record<string, unknown>): ReferenceImageInput[] {
  const refs = resolveReferenceImages(state);
  const legacy = typeof state.reference_image_uri === "string" ? state.reference_image_uri.trim() : "";
  return legacy && refs[0]?.uri === legacy ? refs.slice(1) : refs;
}
