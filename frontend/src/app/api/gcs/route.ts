import { NextRequest, NextResponse } from "next/server";

/**
 * Proxies GCS object downloads so the browser can display them.
 * Usage: /api/gcs?bucket=my-bucket&path=folder/file.html
 *
 * Uses Application Default Credentials (ADC) to authenticate with GCS.
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

  try {
    // Get access token from ADC (gcloud auth application-default)
    let accessToken: string;
    try {
      const tokenRes = await fetch(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        {
          headers: { "Metadata-Flavor": "Google" },
          signal: AbortSignal.timeout(1000),
        }
      );
      if (!tokenRes.ok) throw new Error("metadata server returned non-ok");
      const tokenData = await tokenRes.json();
      accessToken = tokenData.access_token;
    } catch {
      // Fallback: use gcloud CLI token for local dev
      const { execSync } = await import("child_process");
      accessToken = execSync("gcloud auth application-default print-access-token", {
        encoding: "utf-8",
      }).trim();
    }

    const encodedPath = encodeURIComponent(objectPath);
    const gcsUrl = `https://storage.googleapis.com/storage/v1/b/${bucket}/o/${encodedPath}?alt=media`;

    const gcsRes = await fetch(gcsUrl, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });

    if (!gcsRes.ok) {
      return NextResponse.json(
        { error: `GCS error: ${gcsRes.status} ${gcsRes.statusText}` },
        { status: gcsRes.status }
      );
    }

    const contentType = objectPath.endsWith(".html")
      ? "text/html"
      : objectPath.endsWith(".pdf")
        ? "application/pdf"
        : gcsRes.headers.get("content-type") || "application/octet-stream";

    // For HTML files, rewrite GCS image URLs to use this proxy
    if (objectPath.endsWith(".html")) {
      let html = await gcsRes.text();
      // Rewrite mTLS and public GCS URLs to proxy through /api/gcs
      html = html.replace(
        /https:\/\/storage\.mtls\.cloud\.google\.com\/([^/]+)\/([^"?]+)(\?[^"]*)?/g,
        (_match, b, p) =>
          `/api/gcs?bucket=${encodeURIComponent(b)}&path=${encodeURIComponent(p)}`
      );
      return new NextResponse(html, {
        status: 200,
        headers: {
          "Content-Type": contentType,
          "Cache-Control": "private, max-age=300",
        },
      });
    }

    const body = await gcsRes.arrayBuffer();

    return new NextResponse(body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Cache-Control": "private, max-age=300",
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
