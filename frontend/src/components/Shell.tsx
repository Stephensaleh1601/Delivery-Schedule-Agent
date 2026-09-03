"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { cx } from "@/components/ui";

/**
 * Three destinations, ordered as the operation runs.
 *
 * The console used to have six pages of equal weight, one per backend service. That is the
 * architecture's shape, not the business's -- and someone seeing this for the first time has to
 * work out which of six doors answers "what did the agent just do?". Three, in narrative order,
 * answers it by walking left to right.
 *
 * A top bar rather than a side rail: with three destinations a rail only steals width, and both
 * the orders grid and the map want it.
 */
const NAV = [
  { href: "/", label: "Orders", hint: "Every booking and where it stands" },
  { href: "/routes", label: "Daily Routes", hint: "The four days, mapped" },
  { href: "/chat", label: "Customer Chat", hint: "Booking, with the agent's working" },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 border-b border-rail bg-surface/95 backdrop-blur">
        <div className="mx-auto flex h-[58px] max-w-[1400px] items-center gap-8 px-7">
          <Link href="/" className="flex shrink-0 items-baseline gap-2 rounded-[8px]">
            <span className="font-display text-[21px] leading-none text-ink">Dispatch</span>
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
              Majestic Fighters
            </span>
          </Link>

          <nav aria-label="Sections" className="flex items-center gap-1">
            {NAV.map((item) => {
              const active =
                item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  title={item.hint}
                  className={cx(
                    "rounded-[9px] px-3.5 py-2 text-[14px] font-medium transition-colors duration-150",
                    active ? "bg-accent-wash text-accent" : "text-ink-soft hover:bg-sunk",
                  )}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>

          {/* The global "Agent activity" drawer used to sit here. Removed rather than repaired:
              it showed "the agent hasn't run yet" while several runs were persisted, and even
              working it only duplicated the per-message inspector -- which is strictly better,
              because a trace is only meaningful next to the message it produced. */}
        </div>
      </header>

      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}

function PulseDot() {
  return (
    <span className="relative flex h-1.5 w-1.5">
      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
      <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
    </span>
  );
}

/** Shared page frame. One measure and one rhythm across all three sections is most of what makes
 *  a multi-screen tool feel like one product rather than three. */
export function Page({
  title,
  lede,
  actions,
  children,
  wide = false,
}: {
  title: string;
  lede?: string;
  actions?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div
      className={cx(
        "mx-auto flex flex-col gap-7 px-7 py-7",
        wide ? "max-w-[1400px]" : "max-w-[1180px]",
      )}
    >
      <header className="enter flex items-start justify-between gap-8">
        <div className="flex flex-col gap-1.5">
          <h1 className="font-display text-[30px] leading-[1.08] tracking-[-0.01em] text-ink">
            {title}
          </h1>
          {lede && <p className="max-w-[70ch] text-[14px] leading-[1.55] text-ink-muted">{lede}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2 pt-1">{actions}</div>}
      </header>
      {children}
    </div>
  );
}
