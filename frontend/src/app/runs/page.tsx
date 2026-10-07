"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { buttonVariants } from "@/components/ui/button";
import { JudgeAgreement } from "@/components/judge-agreement";
import { RunList, RunListHeader, useRunHistory } from "@/components/run-list";
import { stashDuplicateBrief, type RunRow } from "@/lib/run-history";

export default function RunsPage() {
  const router = useRouter();
  const { rows, loading, error, now } = useRunHistory();

  const duplicate = (row: RunRow) => {
    stashDuplicateBrief(row.brief);
    router.push("/");
  };

  return (
    <div className="mx-auto w-full max-w-6xl px-6 py-8">
      <div className="mb-4 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Runs</h1>
          {!loading && !error && rows.length > 0 && (
            <p className="text-sm text-muted-foreground">
              {rows.length} {rows.length === 1 ? "run" : "runs"}, newest first
            </p>
          )}
          <JudgeAgreement />
        </div>
        <Link href="/" className={buttonVariants()}>
          New run
        </Link>
      </div>

      <section aria-label="Run history" className="rounded-lg border border-border bg-card">
        {loading ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">Loading runs…</p>
        ) : error ? (
          <p role="alert" className="px-4 py-6 text-sm text-mark-fail">
            Couldn&apos;t load runs. Check your connection and reload.
          </p>
        ) : rows.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            No runs yet. Start one from{" "}
            <Link href="/" className="text-primary underline-offset-4 hover:underline">
              New run
            </Link>
            .
          </p>
        ) : (
          <>
            <RunListHeader />
            <RunList rows={rows} now={now} onDuplicate={duplicate} />
          </>
        )}
      </section>
    </div>
  );
}
