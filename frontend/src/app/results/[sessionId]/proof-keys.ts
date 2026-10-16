"use client";

import { createContext, useContext, useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import type { RatingKind } from "@/lib/ratings";

/**
 * Rating keys in the proof viewer. `ProofDetail` owns the one key listener (it
 * applies the shared guards: typing, modifiers, another dialog on top) and passes
 * rating keys to the rating control they target; each `RatingControl` registers
 * itself here. Target: the control holding focus, else the visual (`C` / `V`
 * move focus to the ad copy / visual control).
 */
export interface ProofKeyTarget {
  kind: RatingKind;
  /** The control's root element (decides which control holds focus). */
  root: RefObject<HTMLElement | null>;
  /** Applies a rating key ("p", "f", "0"–"5", "s"); true when it was used. */
  handleKey: (key: string) => boolean;
  /** Moves focus into the control (its verdict group). */
  focus: () => void;
}

export type ProofKeyRegistry = Set<RefObject<ProofKeyTarget>>;

export const ProofKeysContext = createContext<ProofKeyRegistry | null>(null);

/** Registers a rating control with the enclosing proof viewer (no-op outside one). */
export function useProofKeyTarget(target: ProofKeyTarget) {
  const registry = useContext(ProofKeysContext);
  // Latest target without re-registering on every render.
  const latest = useRef(target);
  useLayoutEffect(() => {
    latest.current = target;
  });
  useEffect(() => {
    if (!registry) return;
    registry.add(latest);
    return () => {
      registry.delete(latest);
    };
  }, [registry]);
}

/** The keys `dispatchProofKey` passes to rating controls. */
const RATING_KEYS = new Set(["p", "f", "0", "1", "2", "3", "4", "5", "s"]);

/**
 * Routes one (already guarded, lower-cased) key to the registered rating
 * controls; true when a control used it.
 */
export function dispatchProofKey(registry: ProofKeyRegistry, key: string): boolean {
  const targets = [...registry].map((r) => r.current);
  const byKind = (kind: RatingKind) => targets.find((t) => t.kind === kind);
  if (key === "c" || key === "v") {
    const t = byKind(key === "c" ? "ad_copy" : "visual");
    t?.focus();
    return t !== undefined;
  }
  if (!RATING_KEYS.has(key)) return false;
  const active = document.activeElement;
  const focused = targets.find((t) => active && t.root.current?.contains(active));
  const target = focused ?? byKind("visual");
  return target ? target.handleKey(key) : false;
}
