import { NextRequest, NextResponse } from "next/server";
import { getAccessToken } from "@/lib/gcp-auth";
import { allowedBuckets, gcsMediaUrl, isAllowedBucket } from "@/lib/gcs-server";
import { resolveUser } from "@/lib/iap-identity";
import { isPersonPath, personPathAccess } from "@/lib/person-paths";

/**
 * Sandbox for proxied HTML (the run's HTML gallery): no scripts, no plugins, an
 * opaque origin, and only same-origin (`/api/gcs`-rewritten) or inline images.
 */
const HTML_CSP =
  "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'";

/** Media types a browser renders as a script-capable document. */
const ACTIVE_TYPES = new Set([
  "text/html",
  "application/xhtml+xml",
  "image/svg+xml",
  "text/xml",
  "application/xml",
]);

/**
 * Proxies GCS object downloads so the browser can display them.
 * Usage: /api/gcs?bucket=my-bucket&path=folder/file.html
 *
 * Uses Application Default Credentials (ADC) to authenticate with GCS.
 *
 * The bucket must be a well-formed bucket name (`BUCKET_RE`) and, when
 * `GCS_ALLOWED_BUCKETS` is set, one of the listed buckets; otherwise 400 before any
 * other work (a bucket containing `/`, `?` or `#` could otherwise rewrite the
 * upstream URL and read an arbitrary object). On Cloud Run without that allowlist,
 * person paths are refused (fail closed).
 *
 * Person photos (`person-refs/<slug>/…`) and personalised variants
 * (`…/variants/<slug>/…`) are owner-only: the caller is resolved from the IAP
 * assertion like the /api/adk proxy (missing/invalid on Cloud Run → 401, another
 * user → 404, local dev → allowed), they are never cached and never served as HTML.
 *
 * The content type is the object's own (never derived from the path), every
 * response is `nosniff`, and HTML is served under a sandbox CSP.
 */
export async function GET(request: NextRequest) {
  const bucket = request.nextUrl.searchParams.get("bucket");
  const objectPath = request.nextUrl.searchParams.get("path");

  if (!bucket || !objectPath) {
    return NextResponse.json(
      { error: "Missing bucket or path parameter" },
      { status: 400 }
    );
  }

  if (!isAllowedBucket(bucket)) {
    return NextResponse.json({ error: "Invalid bucket" }, { status: 400 });
  }

  const personal = isPersonPath(objectPath);
  if (personal) {
    if (process.env.K_SERVICE && allowedBuckets().length === 0) {
      console.warn(
        "/api/gcs: refusing a person path because GCS_ALLOWED_BUCKETS is unset on Cloud Run"
      );
      return NextResponse.json({ error: "Invalid bucket" }, { status: 400 });
    }
    const who = await resolveUser(request.headers, {
      onCloudRun: !!process.env.K_SERVICE,
      allowedHd: process.env.IAP_ALLOWED_HD || undefined,
    });
    const access = personPathAccess(objectPath, who);
    if (access === "unauthenticated") {
      return NextResponse.json({ error: "Unauthenticated" }, { status: 401 });
    }
    if (access === "not_found" || objectPath.toLowerCase().endsWith(".html")) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
  }
  const cacheControl = personal ? "private, no-store" : "private, max-age=300";

  try {
    // Get access token from ADC (gcloud auth application-default)
    const accessToken = await getAccessToken();

    const gcsRes = await fetch(gcsMediaUrl(bucket, objectPath), {
      headers: { Authorization: `Bearer ${accessToken}` },
    });

    if (!gcsRes.ok) {
      return NextResponse.json(
        { error: `GCS error: ${gcsRes.status} ${gcsRes.statusText}` },
        { status: gcsRes.status }
      );
    }

    const contentType =
      gcsRes.headers.get("content-type") ||
      (objectPath.toLowerCase().endsWith(".pdf")
        ? "application/pdf"
        : "application/octet-stream");
    const mediaType = contentType.split(";")[0].trim().toLowerCase();

    if (ACTIVE_TYPES.has(mediaType)) {
      // Documents that can run script on this origin: never for person paths,
      // otherwise sandboxed.
      if (personal) {
        return NextResponse.json({ error: "Not found" }, { status: 404 });
      }
    }

    if (mediaType === "text/html") {
      let html = await gcsRes.text();
      // Rewrite mTLS GCS URLs to proxy through /api/gcs (the rewritten links
      // re-enter this route and are validated again).
      html = html.replace(
        /https:\/\/storage\.mtls\.cloud\.google\.com\/([^/]+)\/([^"?]+)(\?[^"]*)?/g,
        (_match, b, p) =>
          `/api/gcs?bucket=${encodeURIComponent(b)}&path=${encodeURIComponent(p)}`
      );
      return new NextResponse(html, {
        status: 200,
        headers: {
          "Content-Type": contentType,
          "Cache-Control": cacheControl,
          "X-Content-Type-Options": "nosniff",
          "Content-Security-Policy": HTML_CSP,
        },
      });
    }

    const body = await gcsRes.arrayBuffer();
    const headers: Record<string, string> = {
      "Content-Type": contentType,
      "Cache-Control": cacheControl,
      "X-Content-Type-Options": "nosniff",
    };
    if (ACTIVE_TYPES.has(mediaType)) headers["Content-Security-Policy"] = HTML_CSP;

    return new NextResponse(body, { status: 200, headers });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
