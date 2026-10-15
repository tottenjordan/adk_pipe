"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";

/** Render errors on the public share viewer. Shows no internals (no message, no digest). */
export default function ShareError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <div className="mx-auto flex w-full max-w-xl flex-col gap-3 px-6 py-24">
      <h1 className="text-2xl font-semibold text-foreground">This page couldn&apos;t load right now</h1>
      <p className="text-base text-muted-foreground">
        Please try again in a moment.
      </p>
      <div>
        <Button onClick={() => retry()}>Try again</Button>
      </div>
    </div>
  );
}
