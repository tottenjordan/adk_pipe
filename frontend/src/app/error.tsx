"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";

/** Render errors anywhere in the app. Never shows `error.message`; the digest matches server logs. */
export default function AppError({
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
      <h1 className="text-2xl font-semibold text-foreground">Something went wrong showing this page</h1>
      <p className="text-base text-muted-foreground">
        Your run carries on on the server. Try again, or reload.
      </p>
      {error.digest && (
        <p className="text-sm text-muted-foreground">Reference: {error.digest}</p>
      )}
      <div>
        <Button onClick={() => retry()}>Try again</Button>
      </div>
    </div>
  );
}
