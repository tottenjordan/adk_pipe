import type { NextRequest } from "next/server";
import { isTimeoutError } from "@/lib/fetch-timeout";
import { loadShareImage } from "@/lib/share-snapshot";

/** `/s/<token>/img/<n>`: streams `shares/<token>/<n>.jpg` (or `.png` for older
 * shares), the object the snapshot names; 404 for anything else. */
export async function GET(_req: NextRequest, ctx: RouteContext<"/s/[token]/img/[n]">) {
  const { token, n } = await ctx.params;
  try {
    return await loadShareImage(token, n);
  } catch (err) {
    if (isTimeoutError(err)) {
      return new Response("Timed out", { status: 504, headers: { "Cache-Control": "no-store" } });
    }
    return new Response("Unavailable", { status: 502, headers: { "Cache-Control": "no-store" } });
  }
}
