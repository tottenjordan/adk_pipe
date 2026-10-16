import { Skeleton } from "@/components/ui/skeleton";

/**
 * The New-run page while its search params resolve (the Suspense fallback):
 * the form card and the recent-runs aside, block for block, so nothing jumps
 * when the form arrives.
 */
export function NewRunSkeleton() {
  return (
    <div
      role="status"
      className="mx-auto grid w-full max-w-6xl gap-6 px-6 py-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start"
    >
      <span className="sr-only">Loading the new-run form…</span>
      <div className="space-y-5 rounded-lg border border-border bg-card p-6">
        <div className="space-y-2">
          <Skeleton className="h-7 w-32" />
          <Skeleton className="h-4 w-3/4" />
        </div>
        <div className="grid gap-2 sm:grid-cols-3">
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
        <div className="grid gap-5 sm:grid-cols-2">
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
        </div>
        <Skeleton className="h-16" />
        <Skeleton className="h-16" />
        <Skeleton className="h-10 w-40" />
      </div>
      <div className="rounded-lg border border-border bg-card">
        <div className="border-b border-border px-4 py-3">
          <Skeleton className="h-4 w-24" />
        </div>
        <div className="space-y-3 p-4">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-4" />
          ))}
        </div>
      </div>
    </div>
  );
}
