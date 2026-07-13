import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // Cloud Workstations serves the dev app from a forwarded *.cloudworkstations.dev
  // host; Next.js 16 blocks cross-origin dev requests unless allow-listed here.
  allowedDevOrigins: [
    "3000-jtotts-cc-station.cluster-bg7xwomlpbbfku2lmsogqimkzi.cloudworkstations.dev",
    "*.cluster-bg7xwomlpbbfku2lmsogqimkzi.cloudworkstations.dev",
  ],
};

export default nextConfig;
