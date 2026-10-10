"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { FieldLabel } from "@/components/field-label";
import { createVariant, listPersonRefs, listVariants } from "@/lib/api";
import type { PersonRef } from "@/lib/person-refs";
import { cn } from "@/lib/utils";
import {
  isPending,
  latestVariant,
  VARIANT_NOTE,
  MAX_POLL_MISSES,
  POLL_STOPPED_MESSAGE,
  VARIANT_POLL_MS,
  variantImageCheck,
  variantImageUrl,
  variantStatusText,
  type VariantRecord,
} from "@/lib/variants";
import { ProofImage } from "@/app/results/[sessionId]/proof-grid";

/**
 * "Personalise" in the results proof dialog: re-render this (uncast) creative with
 * one of the caller's consented people as the hero (`POST /variants`), poll every
 * few seconds while it is queued or rendering, then show the base image and the
 * preview side by side with the preview's image check. A preview is UI only: never
 * judged, rated, shared or deployed to an experiment.
 */
export function PersonalisePanel({
  appName,
  sessionId,
  conceptName,
  baseImageUrl,
  baseAlt,
}: {
  appName: string;
  sessionId: string;
  conceptName: string;
  baseImageUrl: string | null;
  baseAlt: string;
}) {
  const [people, setPeople] = useState<PersonRef[] | null>(null);
  const [peopleFailed, setPeopleFailed] = useState(false);
  const [consentId, setConsentId] = useState("");
  const [variant, setVariant] = useState<{ key: string; record: VariantRecord } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // The caller's people; the first is preselected.
  useEffect(() => {
    listPersonRefs().then(
      (list) => {
        if (!alive.current) return;
        setPeople(list.personRefs);
        setConsentId((current) => current || list.personRefs[0]?.consent_id || "");
      },
      () => {
        if (alive.current) setPeopleFailed(true);
      },
    );
  }, []);

  // An earlier preview of this creative with the chosen person, if any.
  useEffect(() => {
    if (!consentId) return;
    let cancelled = false;
    listVariants(appName, sessionId).then(
      (all) => {
        if (!cancelled && alive.current) setVariant(latestVariant(all, conceptName, consentId));
      },
      () => {
        // No earlier preview to show; rendering still works.
      },
    );
    return () => {
      cancelled = true;
    };
  }, [appName, sessionId, conceptName, consentId]);

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
        const all = await listVariants(appName, sessionId);
        record = all[conceptName]?.[pendingKey];
      } catch {
        record = undefined;
      }
      if (cancelled || !alive.current) return;
      if (record) {
        misses = 0;
        setVariant({ key: pendingKey, record });
        if (!isPending(record.status)) return;
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
  }, [appName, sessionId, conceptName, pendingKey, pollStopped, pollRun]);

  const retryPolling = () => {
    checkNow.current = true;
    setPollStopped(false);
    setPollRun((n) => n + 1);
  };

  const render = useCallback(async () => {
    if (!consentId || submitting) return;
    setSubmitting(true);
    setError(null);
    setPollStopped(false);
    try {
      const created = await createVariant(appName, sessionId, conceptName, consentId);
      if (alive.current) setVariant({ key: created.key, record: created });
    } catch (err) {
      if (alive.current) {
        setError(err instanceof Error ? err.message : "The preview couldn't be made.");
      }
    } finally {
      if (alive.current) setSubmitting(false);
    }
  }, [appName, sessionId, conceptName, consentId, submitting]);

  const record = variant?.record ?? null;
  const pending = isPending(record?.status);
  const imageUrl = variantImageUrl(record);
  const check = variantImageCheck(record);
  const personLabel = people?.find((p) => p.consent_id === consentId)?.label ?? "the chosen person";
  const selectId = `personalise-person-${conceptName.replace(/[^A-Za-z0-9_-]/g, "_")}`;

  return (
    <section aria-labelledby={`${selectId}-heading`} className="border-t border-border pt-4">
      <FieldLabel as="h4" id={`${selectId}-heading`}>
        Personalise
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
                setConsentId(e.target.value);
                setVariant(null);
                setError(null);
                setPollStopped(false);
              }}
              className="h-8 w-full rounded-sm border border-input bg-card px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
            >
              {(people ?? []).map((p) => (
                <option key={p.consent_id} value={p.consent_id}>
                  {p.label}
                </option>
              ))}
            </select>
          </div>
          <Button size="sm" onClick={render} disabled={!consentId || submitting || pending}>
            {submitting ? "Starting…" : pending ? "Rendering…" : "Render preview"}
          </Button>
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
        <div className="mt-3 grid grid-cols-2 gap-2">
          <figure className="min-w-0">
            <ProofImage
              src={baseImageUrl}
              alt={`Original: ${baseAlt}`}
              fit="contain"
              className="aspect-square w-full rounded-sm bg-foreground/[0.04]"
            />
            <figcaption className="mt-1 text-xs text-muted-foreground">Original</figcaption>
          </figure>
          <figure className="min-w-0">
            <ProofImage
              src={imageUrl}
              alt={`Personalised preview with ${personLabel}: ${baseAlt}`}
              fit="contain"
              className="aspect-square w-full rounded-sm bg-foreground/[0.04]"
            />
            <figcaption className="mt-1 text-xs text-muted-foreground">With {personLabel}</figcaption>
          </figure>
        </div>
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
