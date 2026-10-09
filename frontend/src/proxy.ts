import { NextResponse, type NextRequest } from "next/server";
import {
  buildShareCsp,
  decideRoute,
  isShareModeEnabled,
  SHARE_PAGE_HEADERS,
} from "@/lib/share-mode";

/**
 * Next 16 Proxy (the renamed Middleware). It runs before every route — no `matcher` on
 * purpose: in SHARE_MODE it must see every request, static files included. The env is
 * read per request, so the same image serves the IAP app and, with `SHARE_MODE=1`, the
 * public share service.
 */
export function proxy(request: NextRequest) {
  const shareMode = isShareModeEnabled();
  const dev = process.env.NODE_ENV === "development";
  const decision = decideRoute(request.nextUrl.pathname, { shareMode, dev });

  if (decision === "deny") {
    return new NextResponse("Not found", {
      status: 404,
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "X-Content-Type-Options": "nosniff",
      },
    });
  }

  if (decision === "allow") {
    const res = NextResponse.next();
    if (shareMode) res.headers.set("X-Robots-Tag", "noindex, nofollow");
    return res;
  }

  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = buildShareCsp(nonce, dev);
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const res = NextResponse.next({ request: { headers: requestHeaders } });
  res.headers.set("Content-Security-Policy", csp);
  for (const [k, v] of Object.entries(SHARE_PAGE_HEADERS)) res.headers.set(k, v);
  return res;
}
