"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

const ITEMS = [
  { href: "/", label: "New run" },
  { href: "/runs", label: "Runs" },
  { href: "/experiments", label: "Experiments" },
] as const;

/** `/` matches only itself; other sections also own their nested routes. */
export function isNavItemCurrent(pathname: string | null, href: string): boolean {
  if (!pathname) return false;
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Header nav; marks the current section with aria-current and an ink underline. */
export function MainNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="flex items-center gap-1 text-sm">
      {ITEMS.map((item) => {
        const current = isNavItemCurrent(pathname, item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={current ? "page" : undefined}
            className={cn(
              "rounded-sm px-3 py-1.5 transition-colors hover:bg-muted hover:text-foreground",
              current
                ? "font-medium text-foreground underline decoration-2 underline-offset-[6px]"
                : "text-muted-foreground",
            )}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
