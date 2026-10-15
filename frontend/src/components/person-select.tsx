"use client";

import { useEffect, useEffectEvent, useState } from "react";
import Link from "next/link";
import { FieldLabel } from "@/components/field-label";
import { listPersonRefs } from "@/lib/api";
import type { PersonRef } from "@/lib/person-refs";
import type { PersonReferenceInput } from "@/lib/types";

/** The chosen person is no longer among the caller's active consents. */
function isStale(people: PersonRef[], chosen: string, uri: string | undefined): boolean {
  return chosen !== "" && !people.some((p) => p.consent_id === chosen && p.photo_uri === uri);
}

/**
 * "Person (optional)" for creative runs: one of the caller's active person
 * consents (/people). The visual agents decide per concept whether to cast the
 * person (at most a couple of concepts, in person-safe styles only: photographic,
 * 3D, comic, flat illustration, collage); the api re-checks the
 * consent at kick-off. A duplicated brief whose consent is no longer active is
 * dropped once the list loads (or, after it loaded, once the brief arrives).
 */
export function PersonSelect({
  value,
  onChange,
}: {
  value: PersonReferenceInput | null | undefined;
  onChange: (value: PersonReferenceInput | null) => void;
}) {
  const [people, setPeople] = useState<PersonRef[] | null>(null);
  const [failed, setFailed] = useState(false);

  const chosen = value?.consentId ?? "";
  const uri = value?.uri;
  const stale = people !== null && isStale(people, chosen, uri);

  // Reads the latest value/onChange/list without re-running the effects below
  // (a fresh inline onChange from the parent must not re-send the drop).
  const dropIfStale = useEffectEvent((list: PersonRef[] | null = people) => {
    if (list !== null && isStale(list, chosen, uri)) onChange(null);
  });

  useEffect(() => {
    let alive = true;
    listPersonRefs().then(
      (list) => {
        if (!alive) return;
        setPeople(list.personRefs);
        // Checked where the list arrives, not in an effect on `stale`.
        dropIfStale(list.personRefs);
      },
      () => {
        if (alive) setFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  // The parent can also hand in a stale person after the list loaded
  // ("Duplicate here" with a since-revoked consent). Before the list loads the
  // callback above covers it, so this only acts on an already loaded list, and
  // only when the value changes (not on the list load itself).
  useEffect(() => {
    dropIfStale();
  }, [chosen, uri]);

  return (
    <div className="space-y-1.5">
      <FieldLabel as="label" htmlFor="personReference">
        Person (optional)
      </FieldLabel>
      {failed && chosen ? (
        // A person carried over (Duplicate brief) can't be checked against the
        // list: keep it visible so it is never submitted silently.
        <p className="flex items-center gap-2 text-xs text-foreground">
          <span>Person: saved person (couldn&apos;t load your people)</span>
          <button
            type="button"
            onClick={() => onChange(null)}
            className="text-primary underline-offset-4 hover:underline"
          >
            Remove
          </button>
        </p>
      ) : failed ? (
        <p className="text-xs text-muted-foreground">People couldn&apos;t be loaded.</p>
      ) : people !== null && people.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No people registered yet.{" "}
          <Link href="/people" className="text-primary underline-offset-4 hover:underline">
            Register a person
          </Link>
        </p>
      ) : (
        <>
          <select
            id="personReference"
            value={stale ? "" : chosen}
            disabled={people === null}
            onChange={(e) => {
              const person = (people ?? []).find((p) => p.consent_id === e.target.value);
              onChange(person ? { uri: person.photo_uri, consentId: person.consent_id } : null);
            }}
            className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
          >
            <option value="">No person</option>
            {(people ?? []).map((p) => (
              <option key={p.consent_id} value={p.consent_id}>
                {p.label}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            The agents decide per concept whether to feature this person (person-safe
            styles only: photographic, 3D, comic, flat illustration, collage).{" "}
            <Link href="/people" className="text-primary underline-offset-4 hover:underline">
              Manage people
            </Link>
          </p>
        </>
      )}
    </div>
  );
}
