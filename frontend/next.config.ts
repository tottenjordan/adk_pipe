import type { NextConfig } from "next";

// Extra dev-server origins (Next.js 16 blocks cross-origin dev requests from any
// other host), e.g. a Cloud Workstations forwarded host:
//   ALLOWED_DEV_ORIGINS="3000-<station>.<cluster>.cloudworkstations.dev,*.<cluster>.cloudworkstations.dev"
// Comma-separated hostnames, no scheme or port; unset → only localhost.
const allowedDevOrigins = (process.env.ALLOWED_DEV_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  output: "standalone",
  // No `X-Powered-By: Next.js`. The other security headers are set per route by
  // src/proxy.ts (share routes keep their own stricter set).
  poweredByHeader: false,
  ...(allowedDevOrigins.length > 0 && { allowedDevOrigins }),
};

export default nextConfig;
