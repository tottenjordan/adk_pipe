import type { MetadataRoute } from "next";

/**
 * Nothing here is meant for crawlers: the IAP app is private and share links are
 * unlisted (pages also send `X-Robots-Tag: noindex`). Static, so it holds in both
 * normal and SHARE_MODE.
 */
export default function robots(): MetadataRoute.Robots {
  return { rules: { userAgent: "*", disallow: "/" } };
}
