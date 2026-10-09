"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { FieldLabel } from "@/components/field-label";
import { copyStatus, copyText } from "@/components/share-dialog";
import { createPersonRef, listPersonRefs, revokePersonRef } from "@/lib/api";
import { gcsProxyUrl, parseGsUri } from "@/lib/gcs";
import { CONSENT_TEXT, CONSENT_TEXT_VERSION } from "@/lib/person-consent";
import {
  PREFIX_RULE,
  PersonRefError,
  personRefErrorMessage,
  subjectLabel,
  type PersonRef,
  type PersonSubject,
} from "@/lib/person-refs";

function createdLabel(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function errorText(err: unknown): string {
  return err instanceof PersonRefError ? err.message : personRefErrorMessage(null, 0);
}

function Thumbnail({ uri, label }: { uri: string; label: string }) {
  const parsed = parseGsUri(uri);
  if (!parsed) return <div className="h-14 w-14 shrink-0 rounded-sm bg-muted" aria-hidden="true" />;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={gcsProxyUrl(parsed.bucket, parsed.path)}
      alt={`Photo of ${label}`}
      className="h-14 w-14 shrink-0 rounded-sm border border-border object-cover"
    />
  );
}

/**
 * /people: register consented photos of people that runs may feature, and revoke them.
 * The photo is uploaded out of band to the caller's `person-refs/<slug>/` folder; this
 * page records consent for it (runserver/person_refs.py).
 */
export default function PeoplePage() {
  const [people, setPeople] = useState<PersonRef[]>([]);
  const [prefix, setPrefix] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [photoUri, setPhotoUri] = useState("");
  const [label, setLabel] = useState("");
  const [subject, setSubject] = useState<PersonSubject>("self");
  const [adult, setAdult] = useState(false);
  const [publicShare, setPublicShare] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [confirming, setConfirming] = useState<PersonRef | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [copied, setCopied] = useState("");

  useEffect(() => {
    let cancelled = false;
    listPersonRefs()
      .then(({ personRefs, prefix }) => {
        if (cancelled) return;
        setPeople(personRefs);
        setPrefix(prefix);
        if (prefix) setPhotoUri((current) => current || prefix);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(`People could not load. ${errorText(err)}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const setRowError = (id: string, message: string | null) =>
    setRowErrors((prev) => {
      const next = { ...prev };
      if (message) next[id] = message;
      else delete next[id];
      return next;
    });

  const canSubmit = !submitting && adult && label.trim() !== "" && photoUri.trim() !== "";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setFormError(null);
    try {
      const created = await createPersonRef({
        photo_uri: photoUri.trim(),
        label: label.trim(),
        subject,
        adult_attested: adult,
        allow_public_share: publicShare,
        consent_text_version: CONSENT_TEXT_VERSION,
      });
      setPeople((prev) => [created, ...prev]);
      setPhotoUri(prefix ?? "");
      setLabel("");
      setSubject("self");
      setAdult(false);
      setPublicShare(false);
    } catch (err) {
      setFormError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  };

  const revoke = async () => {
    const target = confirming;
    if (!target) return;
    setRevoking(true);
    const remove = () => setPeople((prev) => prev.filter((p) => p.consent_id !== target.consent_id));
    try {
      await revokePersonRef(target.consent_id);
      remove();
    } catch (err) {
      if (err instanceof PersonRefError && err.reason === "person_ref_not_found") remove();
      else setRowError(target.consent_id, errorText(err));
    } finally {
      setRevoking(false);
      setConfirming(null);
    }
  };

  const shownPrefix = prefix ?? PREFIX_RULE;

  return (
    <div className="mx-auto w-full max-w-3xl px-6 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-foreground">People</h1>
        <p className="text-sm text-muted-foreground">
          Photos of people who agreed to appear in your ad previews. Only you can see them.
        </p>
      </div>

      <section aria-labelledby="add-person-title" className="mb-6 rounded-lg border border-border bg-card px-4 py-4">
        <h2 id="add-person-title" className="text-base font-semibold text-foreground">
          Add a person
        </h2>

        <div className="mt-3 space-y-1">
          <FieldLabel as="p">1. Upload the photo to your folder</FieldLabel>
          <div className="flex flex-wrap items-center gap-2">
            <code data-testid="person-prefix" className="min-w-0 break-all rounded-sm bg-muted px-2 py-1 font-mono text-xs text-foreground">
              {shownPrefix}
            </code>
            {prefix && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={async () => setCopied(copyStatus(await copyText(prefix)))}
              >
                Copy
              </Button>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            For example <span className="font-mono">gcloud storage cp photo.jpg {prefix ?? "<your folder>"}</span>.
            A .jpg, .jpeg, .png or .webp image of at most 10 MB.
          </p>
          <p data-testid="person-copy-status" aria-live="polite" className="sr-only">
            {copied}
          </p>
        </div>

        <div className="mt-4 rounded-md border border-border bg-background px-3 py-2.5">
          <FieldLabel as="h3">2. Read the consent</FieldLabel>
          <p data-testid="consent-text" className="mt-1 text-sm text-foreground">
            {CONSENT_TEXT}
          </p>
        </div>

        <form onSubmit={submit} className="mt-4 space-y-4" aria-label="Add a person">
          <div className="space-y-1">
            <FieldLabel as="label" htmlFor="person-photo-uri">
              Photo
            </FieldLabel>
            <Input
              id="person-photo-uri"
              value={photoUri}
              onChange={(e) => setPhotoUri(e.target.value)}
              placeholder={prefix ? `${prefix}photo.jpg` : "gs://…/person-refs/…/photo.jpg"}
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
            />
          </div>
          <div className="space-y-1">
            <FieldLabel as="label" htmlFor="person-label">
              Name
            </FieldLabel>
            <Input
              id="person-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              maxLength={80}
              placeholder="How this person appears in your list"
            />
          </div>
          <fieldset className="space-y-1">
            <FieldLabel as="legend">Who is in the photo</FieldLabel>
            <div className="flex flex-wrap gap-4 text-sm text-foreground">
              {(["self", "third_party_with_consent"] as const).map((value) => (
                <label key={value} className="flex cursor-pointer items-center gap-2">
                  <input
                    type="radio"
                    name="person-subject"
                    value={value}
                    checked={subject === value}
                    onChange={() => setSubject(value)}
                    className="h-4 w-4 accent-primary"
                  />
                  {subjectLabel(value)}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="flex cursor-pointer items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              checked={adult}
              onChange={(e) => setAdult(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary"
            />
            <span>I confirm this person is an adult and agreed to appear in AI-generated ad previews</span>
          </label>
          <label className="flex cursor-pointer items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              checked={publicShare}
              onChange={(e) => setPublicShare(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary"
            />
            <span>They also agreed to appear in public share links</span>
          </label>
          {formError && (
            <p role="alert" className="text-sm text-mark-fail">
              {formError}
            </p>
          )}
          <Button type="submit" disabled={!canSubmit}>
            {submitting ? "Registering…" : "Register photo"}
          </Button>
        </form>
      </section>

      <section aria-labelledby="your-people-title" className="rounded-lg border border-border bg-card">
        <h2 id="your-people-title" className="px-4 pt-3 text-base font-semibold text-foreground">
          Your people
        </h2>
        {loading ? (
          <p className="px-4 py-4 text-sm text-muted-foreground">Loading people…</p>
        ) : loadError ? (
          <p role="alert" className="px-4 py-4 text-sm text-mark-fail">
            {loadError}
          </p>
        ) : people.length === 0 ? (
          <p className="px-4 py-4 text-sm text-muted-foreground">No one yet. Add a person above.</p>
        ) : (
          <ul aria-label="Your people" className="mt-2 divide-y divide-border">
            {people.map((p) => (
              <li key={p.consent_id} className="flex items-center gap-3 px-4 py-3">
                <Thumbnail uri={p.photo_uri} label={p.label} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-foreground">{p.label}</p>
                  <p className="text-xs text-muted-foreground">
                    <span>{subjectLabel(p.subject)}</span>
                    <span aria-hidden="true"> · </span>
                    <span>Public links: {p.allow_public_share ? "yes" : "no"}</span>
                    <span aria-hidden="true"> · </span>
                    <span>
                      Added <time dateTime={p.created_at}>{createdLabel(p.created_at)}</time>
                    </span>
                  </p>
                  {rowErrors[p.consent_id] && (
                    <p role="alert" className="mt-0.5 text-xs text-mark-fail">
                      {rowErrors[p.consent_id]}
                    </p>
                  )}
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Revoke ${p.label}`}
                  onClick={() => {
                    setRowError(p.consent_id, null);
                    setConfirming(p);
                  }}
                >
                  Revoke
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <Dialog
        open={confirming !== null}
        onOpenChange={(next) => {
          if (!next && !revoking) setConfirming(null);
        }}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{confirming ? `Revoke ${confirming.label}?` : "Revoke?"}</DialogTitle>
            <DialogDescription>
              Deletes the photo and every image made with it. This can&apos;t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(null)} disabled={revoking}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={revoke} disabled={revoking}>
              {revoking ? "Revoking…" : "Revoke and delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
