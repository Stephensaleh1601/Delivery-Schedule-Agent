import type { Metadata } from "next";
import { Shell } from "@/components/Shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Dispatch Console",
  description:
    "Route-aware delivery scheduling for Majestic Fighters Furniture Delivery: make a feasible promise, protect it, and recover the day when reality changes.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // suppressHydrationWarning, on these two elements only.
    //
    // Browser extensions stamp attributes onto <html> and <body> before React hydrates -- the
    // reported case was `data-cap-chrome-extension-installed="true"` -- so the server HTML and the
    // DOM React finds genuinely differ, through no fault of ours and with nothing we can render
    // that would match. React's own guidance is to suppress it here.
    //
    // It is deliberately narrow: this suppresses attribute mismatches on the element it is set on
    // and nothing else. A real mismatch anywhere inside the app still warns, which is what makes
    // it safe to leave in -- it silences the extension, not our own bugs.
    <html lang="en-SG" suppressHydrationWarning>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Instrument+Sans:ital,wght@0,400..700;1,400..600&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600&display=swap"
        />
      </head>
      <body suppressHydrationWarning>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
