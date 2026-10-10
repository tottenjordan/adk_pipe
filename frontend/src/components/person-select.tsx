"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { FieldLabel } from "@/components/field-label";
import { listPersonRefs } from "@/lib/api";
import type { PersonRef } from "@/lib/person-refs";
import type { PersonReferenceInput } from "@/lib/types";

/**
 * "Person (optional)" for creative runs: one of the caller's active person
 * consents (/people). The visual agents decide per concept whether to cast the
 * person (at most a couple of photographic concepts); the api re-checks the
 * consent at kick-off. A duplicated brief whose consent is no longer active is
 * dropped once the list loads.
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

  useEffect(() => {
    let alive = true;
    listPersonRefs().then(
      (list) => {
        if (alive) setPeople(list.personRefs);
      },
      () => {
        if (alive) setFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  const chosen = value?.consentId ?? "";
  const stale =
    people !== null &&
    chosen !== "" &&
    !people.some((p) => p.consent_id === chosen && p.photo_uri === value?.uri);

  useEffect(() => {
    if (stale) onChange(null);
  }, [stale, onChange]);

  return (
    <div className="space-y-1.5">
      <FieldLabel as="label" htmlFor="personReference">
        Person (optional)
      </FieldLabel>
      {failed ? (
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
            className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
          >
            <option value="">No person</option>
            {(people ?? []).map((p) => (
              <option key={p.consent_id} value={p.consent_id}>
                {p.label}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            The agents decide per concept whether to feature this person (photographic
            concepts only).{" "}
            <Link href="/people" className="text-primary underline-offset-4 hover:underline">
              Manage people
            </Link>
          </p>
        </>
      )}
    </div>
  );
}
