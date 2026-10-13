"use client";

import { useEffect, useState } from "react";

/**
 * The wall clock in ms, refreshed every `intervalMs`: 0 on the first render
 * (server and first client render agree, so no hydration mismatch), then read in
 * an effect, never during render. Used for the timely creative's expiry
 * (lib/timely.ts), so "Timely" turns "Expired" while the page stays open.
 */
export function useWallClock(intervalMs = 60_000): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const t = setInterval(tick, intervalMs);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [intervalMs]);
  return now;
}
