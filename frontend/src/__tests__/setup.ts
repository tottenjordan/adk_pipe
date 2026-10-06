// jest-dom matchers need a DOM; node-environment files (the default, see
// vitest.config.ts) skip loading them.
if (typeof window !== "undefined") {
  await import("@testing-library/jest-dom/vitest");
}

export {}; // a module, so the top-level await type-checks
