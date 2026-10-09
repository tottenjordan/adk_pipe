"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { MainNav } from "@/components/main-nav";

/** Public share pages (`/s/...`) never show the internal header or navigation. */
export function isShareChromeless(pathname: string | null): boolean {
  if (!pathname) return false;
  return pathname === "/s" || pathname.startsWith("/s/");
}

/** The app header (logo + main nav); omitted on public share pages. */
export function SiteHeader() {
  const pathname = usePathname();
  if (isShareChromeless(pathname)) return null;
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-card shadow-[0_1px_2px_rgb(26_29_33/0.04)]">
      <div className="mx-auto flex h-14 max-w-[1600px] items-center justify-between px-6">
        <Link href="/" className="flex items-center gap-2.5 rounded-sm">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/trend_trawler_banner.png"
            alt=""
            className="h-9 w-auto rounded-sm object-cover"
          />
          <span className="text-lg font-semibold text-foreground">Trend Trawler</span>
        </Link>
        <MainNav />
      </div>
    </header>
  );
}
