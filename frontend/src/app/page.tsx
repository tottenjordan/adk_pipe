"use client";

import { Suspense, useState, useEffect, useRef, useTransition } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { FieldLabel } from "@/components/field-label";
import { LearnFromRatingsToggle } from "@/components/learn-from-ratings-toggle";
import { PersonSelect } from "@/components/person-select";
import { SAFER_ANGLE_ADVICE, SaferAngleToggle } from "@/components/safer-angle-toggle";
import { SENSITIVE_TREND_ADVICE } from "@/components/sensitive-chip";
import { CampaignFields } from "@/components/new-run/campaign-fields";
import { NewRunSkeleton } from "@/components/new-run/new-run-skeleton";
import { RecentRunsAside } from "@/components/new-run/recent-runs-aside";
import {
  FIELD_IDS,
  FieldError,
  fieldErrors,
  missingRequired,
  requiredProps,
  type RequiredField,
} from "@/components/new-run/required-fields";
import {
  ReferenceImagesFieldset,
  newReferenceRow,
} from "@/components/new-run/reference-images-fieldset";
import {
  VisualDirectionFieldset,
  hasVisualDirection,
} from "@/components/new-run/visual-direction-fieldset";
import { createSession, SELF_USER_ID } from "@/lib/api";
import { AGENTS, isAgentId, isCreativeAgent, submitLabel } from "@/lib/agents";
import { isFormValid } from "@/lib/form-validation";
import { buildInitialState } from "@/lib/initial-state";
import { invalidReferenceUris } from "@/lib/reference-images";
import { takeDuplicateBrief, type Brief, type RunRow } from "@/lib/run-history";
import { sanitizeTrendContext } from "@/lib/trend-context";
import { contextSensitivity } from "@/lib/trend-flags";
import type { CampaignInput } from "@/lib/types";

const EMPTY_FORM: CampaignInput = {
  agent: "trend_scout",
  brand: "",
  targetAudience: "",
  targetProduct: "",
  keySellingPoints: "",
  targetSearchTrend: "",
  interactiveTrendPick: false,
  learnFromRatings: false,
  saferAngle: false,
  personReference: null,
  referenceImageUri: "",
  referenceImageRole: "",
  extraReferenceImages: [],
  visualIntent: "",
  brandColors: "",
  visualStylePreference: "",
  visualAvoid: "",
  visualAspectRatio: "",
};

export default function Home() {
  return (
    <Suspense fallback={<NewRunSkeleton />}>
      <HomeContent />
    </Suspense>
  );
}

function HomeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const formRef = useRef<HTMLFormElement>(null);
  // Pending from submit until the run page lands: a re-enabled button
  // mid-navigation would let a second click create an orphan session.
  const [starting, startRun] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<CampaignInput>(EMPTY_FORM);
  const [visualOpen, setVisualOpen] = useState(false);
  const [duplicated, setDuplicated] = useState(false);
  // Required fields whose "missing" message may show: left by the user, or all
  // of them after a blocked Ctrl/⌘+Enter.
  const [shownErrors, setShownErrors] = useState<ReadonlySet<RequiredField>>(new Set());
  const leaveField = (field: RequiredField) =>
    setShownErrors((prev) => (prev.has(field) ? prev : new Set(prev).add(field)));

  const applyBrief = (brief: Brief) => {
    setForm({
      ...EMPTY_FORM,
      ...brief,
      extraReferenceImages: (brief.extraReferenceImages ?? []).map((r) => newReferenceRow(r)),
    });
    setVisualOpen(hasVisualDirection(brief));
    setDuplicated(true);
  };

  // Pre-fill from URL params (when clicking a trend card on a trend_scout run).
  // Applied while rendering whenever the params change (React's "storing
  // information from previous renders" pattern), not in an effect.
  const [prefilledFrom, setPrefilledFrom] = useState<typeof searchParams | null>(null);
  if (prefilledFrom !== searchParams) {
    setPrefilledFrom(searchParams);
    const agent = searchParams.get("agent");
    const brand = searchParams.get("brand");
    const targetAudience = searchParams.get("targetAudience");
    const targetProduct = searchParams.get("targetProduct");
    const keySellingPoints = searchParams.get("keySellingPoints");
    const targetSearchTrend = searchParams.get("targetSearchTrend");
    // A link can carry anything: keep only the allowlisted, capped trend_context shape.
    const trendContext = sanitizeTrendContext(searchParams.get("trendContext"));
    const trendDetectedOn = searchParams.get("trendDetectedOn");

    if (agent || brand || targetSearchTrend) {
      setForm((prev) => ({
        ...prev,
        ...(isAgentId(agent) && { agent }),
        ...(brand && { brand }),
        ...(targetAudience && { targetAudience }),
        ...(targetProduct && { targetProduct }),
        ...(keySellingPoints && { keySellingPoints }),
        ...(targetSearchTrend && { targetSearchTrend }),
        ...(targetSearchTrend && trendContext && { trendContext }),
        ...(targetSearchTrend && trendDetectedOn && { trendDetectedOn }),
        // A newly carried trend starts with the respectful-angle opt-in off.
        ...(targetSearchTrend && { saferAngle: false }),
      }));
    }
  }

  // Pre-fill from a "Duplicate brief" on the Runs page (one-shot sessionStorage
  // hand-off: the brief carries visual-direction fields too long for a URL).
  useEffect(() => {
    const brief = takeDuplicateBrief();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sessionStorage is client-only and read after hydration (a render-time read would mismatch the server HTML)
    if (brief) applyBrief(brief);
  }, []);

  const isCreative = isCreativeAgent(form.agent);
  const isValid = isFormValid(form);
  const errors = fieldErrors(form, shownErrors);

  // Runs inside the startRun transition, so `starting` stays true until
  // router.push lands (push is itself a transition) or the start fails.
  const createAndOpenRun = async () => {
    try {
      const userId = SELF_USER_ID;
      // Seed the session's initial state: the agent (for run history), the
      // trend_scout trend-pick opt-in, or the creative agents' campaign fields
      // and optional visual-intent fields. See buildInitialState.
      const initialState = buildInitialState(form);
      const session = await createSession(form.agent, userId, initialState);

      // Build the user message with campaign metadata (a human-readable echo:
      // the creative agents read the seeded state, see buildInitialState)
      let message = `Brand Name: "${form.brand}"\nTarget Audience: "${form.targetAudience}"\nTarget Product: "${form.targetProduct}"\nKey Selling Points: "${form.keySellingPoints}"`;
      if (isCreative && form.targetSearchTrend) {
        message += `\ntarget_search_trends: "${form.targetSearchTrend}"`;
      }

      // Store message in sessionStorage to avoid URL length limits
      sessionStorage.setItem(`run:${session.id}`, JSON.stringify({ message }));

      const params = new URLSearchParams({
        app: form.agent,
        userId,
      });
      router.push(`/run/${session.id}?${params.toString()}`);
    } catch (err) {
      setError(
        err instanceof Error
          ? `Couldn't start the run: ${err.message}. Check your connection and try again.`
          : "Couldn't start the run. Check your connection and try again.",
      );
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!isValid || starting) return;
    setError(null);
    startRun(createAndOpenRun);
  };

  // Cmd/Ctrl+Enter submits from anywhere in the form (including textareas).
  // Blocked by a missing field, it flags every missing field and moves focus
  // to the first, so the reason is announced where it can be fixed.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLFormElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (isValid && !starting) {
        formRef.current?.requestSubmit();
        return;
      }
      const missing = missingRequired(form);
      if (missing.length === 0) return;
      setShownErrors(new Set(missing));
      document.getElementById(FIELD_IDS[missing[0]])?.focus();
    }
  };

  const duplicateHere = (row: RunRow) => {
    applyBrief(row.brief);
    window.scrollTo({ top: 0 });
    document.getElementById("brand")?.focus({ preventScroll: true });
  };

  const prefilledTrend = searchParams.get("targetSearchTrend");
  // trend_scout flagged the carried-over trend as a sensitive event (cleared
  // with the context when the trend is edited).
  const sensitivity = contextSensitivity(form.trendContext);

  return (
    <div className="mx-auto grid w-full max-w-6xl gap-6 px-6 py-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
      <section
        aria-labelledby="new-run-heading"
        className="rounded-lg border border-border bg-card p-6"
      >
        <div className="mb-5 space-y-1">
          <h1 id="new-run-heading" className="text-2xl font-semibold text-foreground">
            New run
          </h1>
          <p className="text-sm text-muted-foreground">
            Describe your campaign, pick what to run, and we&apos;ll turn today&apos;s
            trends into ad creatives.
          </p>
        </div>

        {(prefilledTrend || duplicated) && (
          <p className="mb-5 rounded-md border border-border bg-muted/50 px-3 py-2 text-sm text-foreground">
            {duplicated ? (
              "Brief copied from a past run. Edit anything before you start."
            ) : (
              <>
                Pre-filled with trend <strong>{prefilledTrend}</strong> from your trend
                scout run.
              </>
            )}
          </p>
        )}
        {isCreative && sensitivity.sensitive && (
          <div className="mb-5 space-y-2.5 rounded-md border border-mark-pending/40 bg-mark-pending/5 px-3 py-2 text-sm text-foreground">
            <p role="alert">
              <strong className="font-semibold">Sensitive trend</strong>
              {sensitivity.reason ? `: ${sensitivity.reason}. ` : ". "}
              {form.saferAngle ? SAFER_ANGLE_ADVICE : SENSITIVE_TREND_ADVICE}
            </p>
            <SaferAngleToggle
              checked={form.saferAngle ?? false}
              onChange={(checked) => setForm({ ...form, saferAngle: checked })}
            />
          </div>
        )}

        <form
          ref={formRef}
          onSubmit={handleSubmit}
          onKeyDown={handleKeyDown}
          className="space-y-5"
        >
          <fieldset className="space-y-2">
            <FieldLabel as="legend" className="mb-2">
              What to run
            </FieldLabel>
            <div className="grid gap-2 sm:grid-cols-3">
              {AGENTS.map((a) => (
                <label
                  key={a.id}
                  className="relative flex cursor-pointer flex-col gap-1 rounded-md border border-border bg-background px-3 py-2.5 transition-colors hover:border-foreground/30 has-[:checked]:border-primary has-[:checked]:bg-card has-[:checked]:ring-1 has-[:checked]:ring-primary has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50"
                >
                  <input
                    type="radio"
                    name="agent"
                    value={a.id}
                    checked={form.agent === a.id}
                    onChange={() => setForm({ ...form, agent: a.id })}
                    className="sr-only"
                  />
                  <span className="text-sm font-semibold text-foreground">{a.label}</span>
                  <span className="text-xs leading-snug text-muted-foreground">
                    {a.description}
                  </span>
                  <span className="mt-auto flex flex-wrap gap-x-3 pt-1 text-xs text-muted-foreground">
                    <span className="tabular-nums">{a.duration}</span>
                    <span>{a.pauses}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {form.agent === "trend_scout" && (
            <label
              htmlFor="interactive-trend-pick"
              className="flex items-start gap-3 rounded-md border border-border bg-background px-3 py-2.5 cursor-pointer hover:border-foreground/20 transition-colors"
            >
              <input
                id="interactive-trend-pick"
                type="checkbox"
                checked={form.interactiveTrendPick ?? false}
                onChange={(e) =>
                  setForm({ ...form, interactiveTrendPick: e.target.checked })
                }
                className="mt-0.5 h-4 w-4 shrink-0 rounded-sm border-border accent-primary cursor-pointer"
              />
              <span className="space-y-0.5">
                <span className="block text-sm font-medium text-foreground">
                  Let me pick the trends myself
                </span>
                <span className="block text-xs text-muted-foreground">
                  Pause after gathering the top ~25 trends so you can choose which
                  to keep.
                </span>
              </span>
            </label>
          )}

          <CampaignFields form={form} setForm={setForm} errors={errors} onLeave={leaveField} />

          {isCreative && (
            <div className="grid gap-5 sm:grid-cols-2">
              <div className="space-y-1.5">
                <FieldLabel as="label" htmlFor="trend">
                  Target search trend
                </FieldLabel>
                <Input
                  id="trend"
                  placeholder='e.g., "tswift engaged"'
                  value={form.targetSearchTrend}
                  onChange={(e) =>
                    // A different trend: the carried-over trend_scout context
                    // and detection date no longer describe it (and the
                    // respectful-angle opt-in goes with the sensitive context).
                    setForm({
                      ...form,
                      targetSearchTrend: e.target.value,
                      trendContext: undefined,
                      trendDetectedOn: undefined,
                      saferAngle: false,
                    })
                  }
                  onBlur={() => leaveField("targetSearchTrend")}
                  {...requiredProps("targetSearchTrend", errors.targetSearchTrend)}
                />
                <FieldError field="targetSearchTrend" error={errors.targetSearchTrend} />
              </div>

              <PersonSelect
                value={form.personReference}
                onChange={(personReference) =>
                  setForm((prev) => ({ ...prev, personReference }))
                }
              />

              <div className="sm:col-span-2">
                <ReferenceImagesFieldset form={form} setForm={setForm} />
              </div>
            </div>
          )}

          {isCreative && (
            <LearnFromRatingsToggle
              brand={form.brand}
              checked={form.learnFromRatings ?? false}
              onChange={(checked) => setForm({ ...form, learnFromRatings: checked })}
            />
          )}

          {isCreative && (
            <VisualDirectionFieldset
              form={form}
              setForm={setForm}
              open={visualOpen}
              onOpenChange={setVisualOpen}
            />
          )}

          {error && (
            <div role="alert" className="rounded-md bg-destructive/5 border border-destructive/30 px-4 py-3">
              <p className="text-sm text-destructive">{error}</p>
            </div>
          )}

          <div className="flex items-center gap-4">
            <Button
              type="submit"
              size="lg"
              className="h-10 px-5 text-sm font-semibold"
              disabled={!isValid || starting}
              aria-describedby="submit-hint"
            >
              {starting ? (
                <span className="flex items-center gap-2">
                  <span className="h-4 w-4 rounded-full border-2 border-primary-foreground/30 border-t-primary-foreground animate-spin" />
                  Starting…
                </span>
              ) : (
                submitLabel(form.agent)
              )}
            </Button>
            <span id="submit-hint" aria-live="polite" className="text-xs text-muted-foreground">
              {isValid ? (
                <>
                  or press <Kbd>Ctrl</Kbd>/<Kbd>⌘</Kbd> + <Kbd>Enter</Kbd>
                </>
              ) : isCreative && invalidReferenceUris(form).length ? (
                "Fix the reference image URL to start."
              ) : isCreative ? (
                "Fill in the campaign fields and a trend to start."
              ) : (
                "Fill in the campaign fields to start."
              )}
            </span>
          </div>
        </form>
      </section>

      <RecentRunsAside onDuplicate={duplicateHere} />
    </div>
  );
}
