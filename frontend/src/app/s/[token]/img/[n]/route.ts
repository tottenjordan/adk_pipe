import type { NextRequest } from "next/server";
import { loadShareImage } from "@/lib/share-snapshot";

/** `/s/<token>/img/<n>`: streams `shares/<token>/<n>.png`; 404 for anything else. */
export async function GET(_req: NextRequest, ctx: RouteContext<"/s/[token]/img/[n]">) {
  const { token, n } = await ctx.params;
  try {
    return await loadShareImage(token, n);
  } catch {
    return new Response("Unavailable", { status: 502, headers: { "Cache-Control": "no-store" } });
  }
}
