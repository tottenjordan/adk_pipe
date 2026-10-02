import type { Metadata } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import { MainNav } from "@/components/main-nav";
import "./globals.css";

// Variable Archivo with the width axis: condensed heavy weights for creative
// headlines/scores, normal width for body text.
const archivo = Archivo({
  variable: "--font-archivo",
  subsets: ["latin"],
  axes: ["wdth"],
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Trend Trawler",
  description: "Trend-to-creative ad generation powered by multi-agent AI",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${archivo.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <header className="sticky top-0 z-40 border-b border-border bg-card shadow-[0_1px_2px_rgb(26_29_33/0.04)]">
          <div className="mx-auto flex h-14 max-w-[1600px] items-center justify-between px-6">
            <Link href="/" className="flex items-center gap-2.5 rounded-sm">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/trend_trawler_banner.png"
                alt=""
                className="h-9 w-auto rounded-sm object-cover"
              />
              <span className="text-lg font-semibold text-foreground">
                Trend Trawler
              </span>
            </Link>
            <MainNav />
          </div>
        </header>
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
