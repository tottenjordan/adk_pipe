import type { Metadata } from "next";
import { Sora, JetBrains_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const sora = Sora({
  variable: "--font-sora",
  subsets: ["latin"],
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
      className={`${sora.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <header className="glass-strong sticky top-0 z-40">
          <div className="mx-auto flex h-14 max-w-[1600px] items-center justify-between px-6">
            <Link href="/" className="flex items-center gap-2.5 group">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/trend_trawler_banner.png"
                alt="Trend Trawler"
                className="h-9 w-auto rounded-md object-cover"
              />
              <span className="text-lg font-semibold tracking-tight text-foreground">
                Trend Trawler
              </span>
            </Link>
            <nav className="flex items-center gap-4 text-sm text-muted-foreground">
              <Link
                href="/"
                className="rounded-md px-3 py-1.5 transition-colors hover:text-foreground hover:bg-black/5"
              >
                New Run
              </Link>
            </nav>
          </div>
        </header>
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
