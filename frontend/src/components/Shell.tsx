"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { cx } from "@/components/ui";

/**
 * The persistent rail.
 *
 * Ordered as the operation runs, not alphabetically: what's happening now, then the customer
 * conversation, then planning, the route, disruption, and finally the record of what the agent
 * did. Following it top to bottom is the demo.
 */
const NAV = [
  { href: "/", label: "Overview", hint: "Today across the horizon" },
  { href: "/conversation", label: "Conversation", hint: "Customer booking" },
  { href: "/planning", label: "Planning", hint: "Slot evaluation" },
  { href: "/routes", label: "Route plan", hint: "Versions & comparison" },
  { href: "/recovery", label: "Recovery", hint: "Delays & replacements" },
  { href: "/activity", label: "Agent activity", hint: "Tools & decisions" },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen">
      <nav
        aria-label="Sections"
        className="sticky top-0 flex h-screen w-[248px] shrink-0 flex-col gap-7 border-r border-rail bg-surface px-5 py-6"
      >
        <Link href="/" className="flex flex-col gap-0.5 rounded-[9px] px-1">
          <span className="font-display text-[22px] leading-none text-ink">Dispatch</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-ink-faint">
            Majestic Fighters
          </span>
        </Link>

        <ul className="flex flex-col gap-0.5">
          {NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cx(
                    "group flex flex-col gap-[1px] rounded-[9px] px-3 py-2 transition-colors duration-150",
                    active ? "bg-accent-wash" : "hover:bg-sunk",
                  )}
                >
                  <span
                    className={cx(
                      "text-[13.5px] font-medium leading-[1.35]",
                      active ? "text-accent" : "text-ink-soft",
                    )}
                  >
                    {item.label}
                  </span>
                  <span
                    className={cx(
                      "text-[11.5px] leading-[1.35]",
                      active ? "text-accent/70" : "text-ink-faint",
                    )}
                  >
                    {item.hint}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>

        <div className="mt-auto flex flex-col gap-1.5 rounded-[11px] border border-rail bg-sunk/60 px-3 py-2.5">
          <span className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
            Live data
          </span>
          <p className="text-[11.5px] leading-[1.45] text-ink-muted">
            Every figure on these screens is read from the dispatch service. Nothing is simulated
            in the browser.
          </p>
        </div>
      </nav>

      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}

/** Shared page frame: a title block, then content. Keeps the measure and rhythm identical
 *  across screens, which is most of what makes a multi-screen tool feel like one product. */
export function Page({
  title,
  lede,
  actions,
  children,
}: {
  title: string;
  lede?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto flex max-w-[1120px] flex-col gap-8 px-9 py-9">
      <header className="enter flex items-start justify-between gap-8">
        <div className="flex flex-col gap-1.5">
          <h1 className="font-display text-[32px] leading-[1.08] tracking-[-0.01em] text-ink">
            {title}
          </h1>
          {lede && <p className="max-w-[64ch] text-[14px] leading-[1.55] text-ink-muted">{lede}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2 pt-1">{actions}</div>}
      </header>
      {children}
    </div>
  );
}
