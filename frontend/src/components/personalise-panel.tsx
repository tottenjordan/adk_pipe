"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { createVariant, listPersonRefs, listVariants } from "@/lib/api";
import type { PersonRef } from "@/lib/person-refs";
import { cn } from "@/lib/utils";
import {
  castabilityNote,
  castConsentToSkip,
  isPending,
  NO_OTHER_PERSON_NOTE,
  latestVariant,
  VARIANT_NOTE,
  MAX_POLL_MISSES,
  POLL_STOPPED_MESSAGE,
  VARIANT_POLL_MS,
  variantImageCheck,
  variantImageUrl,
  variantStatusText,
  type Castability,
  type VariantRecord,
  type VariantsListing,
} from "@/lib/variants";
import { VariantCompare } from "@/components/variant-compare";

/** The results page's personalisation data, shared by every proof's panel. */
export interface PersonalisationData {
  /** The caller's people (null until loaded or when the load failed). */
  people: PersonRef[] | null;
  peopleFailed: boolean;
  /** The session's variants + castability (null until loaded). */
  listing: VariantsListing | null;
  /** Re-read the variants (after a create or a finished poll). */
  refreshVariants: () => void;
}

type Loaded = {
  key: string;
  people: PersonRef[] | null;
  peopleFailed: boolean;
  listing: VariantsListing | null;
};

/**
 * Load the caller's people and the session's variants ONCE for the whole results
 * page (the proof dialog remounts the panel per creative, and `listPersonRefs` is
 * expensive server-side). Nothing loads until `enabled` (the proof dialog is open on
 * a creative app); `refreshVariants` re-reads only the variants.
 */
export function usePersonalisationData(
  appName: string,
  sessionId: string,
  enabled = true,
): PersonalisationData {
  const key = `${appName}/${sessionId}`;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const loadedFor = useRef<string | null>(null);
  // Bumped per variants request: an older response never overwrites a newer one.
  const variantsRequest = useRef(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const update = useCallback(
    (patch: Partial<Omit<Loaded, "key">>) =>
      setLoaded((prev) => ({
        ...(prev?.key === key ? prev : { key, people: null, peopleFailed: false, listing: null }),
        ...patch,
      })),
    [key],
  );

  const refreshVariants = useCallback(() => {
    const request = ++variantsRequest.current;
    listVariants(appName, sessionId).then(
      (listing) => {
        if (alive.current && request === variantsRequest.current) update({ listing });
      },
      () => {
        // Keep the last listing; rendering still works without it.
      },
    );
  }, [appName, sessionId, update]);

  useEffect(() => {
    if (!enabled || loadedFor.current === key) return;
    loadedFor.current = key;
    listPersonRefs().then(
      (list) => {
        if (alive.current && loadedFor.current === key) update({ people: list.personRefs });
      },
      () => {
        if (alive.current && loadedFor.current === key) update({ peopleFailed: true });
      },
    );
    refreshVariants();
  }, [enabled, key, refreshVariants, update]);

  const current = loaded?.key === key ? loaded : null;
  return {
    people: current?.people ?? null,
    peopleFailed: current?.peopleFailed ?? false,
    listing: current?.listing ?? null,
    refreshVariants,
  };
}

/** The slice of {@link PersonalisationData} one creative's panel needs. */
export function personalisationProps(data: PersonalisationData, conceptName: string) {
  return {
    people: data.people,
    peopleFailed: data.peopleFailed,
    castable: data.listing?.castable?.[conceptName],
    personSafeStyles: data.listing?.personSafeStyles ?? [],
    variants: data.listing?.variants[conceptName],
    onVariantsChanged: data.refreshVariants,
  };
}

/**
 * "Personalise" in the results proof dialog: re-render this creative with one of
 * the caller's consented people as the hero (`POST /variants`). For a creative that
 * already casts a person (`castable` `mode: "swap"`) it reads "Swap person" and
 * leaves that person out of the picker. It polls every
 * few seconds while it is queued or rendering, then show the base image and the
 * preview side by side with the preview's image check. A preview is UI only: never
 * judged, rated, shared or deployed to an experiment.
 *
 * The people, castability and earlier variants come from the page
 * ({@link usePersonalisationData}); the panel only submits and polls.
 */
export function PersonalisePanel({
  appName,
  sessionId,
  conceptName,
  baseImageUrl,
  baseAlt,
  people,
  peopleFailed,
  castable: castEntry,
  personSafeStyles,
  variants,
  onVariantsChanged,
}: {
  appName: string;
  sessionId: string;
  conceptName: string;
  baseImageUrl: string | null;
  baseAlt: string;
  /** The caller's people (null while loading). */
  people: PersonRef[] | null;
  peopleFailed: boolean;
  /** Whether the api would accept this creative (undefined until loaded / older api). */
  castable: Castability | undefined;
  personSafeStyles: string[];
  /** This creative's earlier variants by key (undefined until loaded). */
  variants: Record<string, VariantRecord> | undefined;
  /** A variant was created or finished: the page re-reads the variants. */
  onVariantsChanged?: () => void;
}) {
  // The person the caller picked ("" = the first one offered).
  const [picked, setPicked] = useState("");
  // The preview started or polled here; wins over the page's earlier preview, so a
  // late listing never resets a render queued by a fast click.
  const [live, setLive] = useState<{ key: string; record: VariantRecord } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // A swap leaves the person already in the creative out of the picker.
  const swap = castEntry?.mode === "swap";
  const skip = castConsentToSkip(castEntry);
  const options = (people ?? []).filter((p) => !skip || p.consent_id !== skip);
  const consentId = options.some((p) => p.consent_id === picked) ? picked : (options[0]?.consent_id ?? "");
  const noOther = swap && people !== null && people.length > 0 && options.length === 0;

  // An earlier preview of this creative with the chosen person, if any.
  const earlier = consentId ? latestVariant({ [conceptName]: variants ?? {} }, conceptName, consentId) : null;
  const variant = live ?? earlier;

  // Poll while the preview is queued or rendering; stops on unmount, a new key, or
  // after MAX_POLL_MISSES consecutive misses (an error or a missing record), when a
  // Retry button takes over. `pollRun` restarts the loop (Retry checks at once).
  const pendingKey = variant && isPending(variant.record.status) ? variant.key : null;
  const [pollStopped, setPollStopped] = useState(false);
  const [pollRun, setPollRun] = useState(0);
  const checkNow = useRef(false);
  useEffect(() => {
    if (!pendingKey || pollStopped) return;
    let cancelled = false;
    let misses = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      let record: VariantRecord | undefined;
      try {
        const listing = await listVariants(appName, sessionId);
        record = listing.variants[conceptName]?.[pendingKey];
      } catch {
        record = undefined;
      }
      if (cancelled || !alive.current) return;
      if (record) {
        misses = 0;
        setLive({ key: pendingKey, record });
        if (!isPending(record.status)) {
          onVariantsChanged?.();
          return;
        }
      } else if (++misses >= MAX_POLL_MISSES) {
        setPollStopped(true);
        return;
      }
      timer = setTimeout(tick, VARIANT_POLL_MS);
    };
    timer = setTimeout(tick, checkNow.current ? 0 : VARIANT_POLL_MS);
    checkNow.current = false;
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [appName, sessionId, conceptName, pendingKey, pollStopped, pollRun, onVariantsChanged]);

  const retryPolling = () => {
    checkNow.current = true;
    setPollStopped(false);
    setPollRun((n) => n + 1);
  };

  const ineligible =
    castabilityNote(castEntry, personSafeStyles) ?? (noOther ? NO_OTHER_PERSON_NOTE : null);

  // A plain handler: the React Compiler memoizes it (a manual useCallback over
  // the derived `consentId` couldn't be preserved).
  const render = async () => {
    if (!consentId || submitting || ineligible) return;
    setSubmitting(true);
    setError(null);
    setPollStopped(false);
    try {
      const created = await createVariant(appName, sessionId, conceptName, consentId);
      if (alive.current) setLive({ key: created.key, record: created });
      onVariantsChanged?.();
    } catch (err) {
      if (alive.current) {
        setError(err instanceof Error ? err.message : "The preview couldn't be made.");
      }
    } finally {
      if (alive.current) setSubmitting(false);
    }
  };

  const record = variant?.record ?? null;
  const pending = isPending(record?.status);
  const imageUrl = variantImageUrl(record);
  const check = variantImageCheck(record);
  const personLabel = people?.find((p) => p.consent_id === consentId)?.label ?? "the chosen person";
  const selectId = `personalise-person-${conceptName.replace(/[^A-Za-z0-9_-]/g, "_")}`;

  return (
    <section aria-labelledby={`${selectId}-heading`} className="border-t border-border pt-4">
      <FieldLabel as="h4" id={`${selectId}-heading`}>
        {swap ? "Swap person" : "Personalise"}
      </FieldLabel>

      {peopleFailed ? (
        <p className="mt-1.5 text-xs text-muted-foreground">People couldn&apos;t be loaded.</p>
      ) : people !== null && people.length === 0 ? (
        <p className="mt-1.5 text-xs text-muted-foreground">
          Preview this creative with a person who agreed to appear.{" "}
          <Link href="/people" className="text-primary underline-offset-4 hover:underline">
            Register a person
          </Link>
        </p>
      ) : (
        <div className="mt-1.5 flex flex-wrap items-end gap-2">
          <div className="min-w-0 flex-1 space-y-1">
            <label htmlFor={selectId} className="block text-xs text-muted-foreground">
              Person
            </label>
            <select
              id={selectId}
              value={consentId}
              disabled={people === null || submitting}
              onChange={(e) => {
                setPicked(e.target.value);
                setLive(null);
                setError(null);
                setPollStopped(false);
              }}
              className="h-8 w-full rounded-sm border border-input bg-card px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
            >
              {options.map((p) => (
                <option key={p.consent_id} value={p.consent_id}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>
          <Button
            size="sm"
            onClick={render}
            disabled={!consentId || submitting || pending || ineligible !== null}
            aria-describedby={ineligible ? `${selectId}-ineligible` : undefined}
          >
            {submitting ? "Starting…" : pending ? "Rendering…" : "Render preview"}
          </Button>
          {ineligible && (
            <p id={`${selectId}-ineligible`} className="w-full text-xs text-muted-foreground">
              {ineligible === NO_OTHER_PERSON_NOTE ? (
                <>
                  Register another person on the{" "}
                  <Link href="/people" className="text-primary underline-offset-4 hover:underline">
                    People page
                  </Link>{" "}
                  to swap.
                </>
              ) : (
                ineligible
              )}
            </p>
          )}
        </div>
      )}

      <p className="mt-1.5 text-xs text-muted-foreground">{VARIANT_NOTE}</p>

      <p role="status" aria-live="polite" className="mt-2 text-xs text-foreground empty:hidden">
        {record ? variantStatusText(record) : ""}
      </p>
      {pollStopped && pending && (
        <div role="alert" className="mt-2 flex flex-wrap items-center gap-2 text-xs text-mark-fail">
          <span>{POLL_STOPPED_MESSAGE}</span>
          <Button size="xs" variant="outline" onClick={retryPolling}>
            Retry
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-mark-fail">
          {error}
        </p>
      )}

      {record?.status === "done" && (
        <VariantCompare
          original={{ src: baseImageUrl, alt: `Original: ${baseAlt}`, caption: "Original" }}
          preview={{
            src: imageUrl,
            alt: `Personalised preview with ${personLabel}: ${baseAlt}`,
            caption: `With ${personLabel}`,
          }}
        />
      )}

      {check && (
        <div className="mt-2 text-xs">
          <span className="text-muted-foreground">Image check: </span>
          <span className={cn("font-medium", check.passed ? "text-mark-pass" : "text-mark-fail")}>
            {check.passed ? "passed" : "issues"}
          </span>
          {!check.passed && check.issues.length > 0 && (
            <ul className="mt-1 list-disc space-y-0.5 pl-4 leading-snug text-foreground/85">
              {check.issues.map((issue, i) => (
                <li key={i}>{issue}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
