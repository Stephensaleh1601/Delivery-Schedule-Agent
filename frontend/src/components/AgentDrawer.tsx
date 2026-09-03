"use client";

import { useEffect } from "react";
import { RunCard } from "@/components/AgentTrace";
import { Button, EmptyPanel, ErrorPanel, LoadingPanel, cx } from "@/components/ui";
import { dispatch } from "@/lib/api";
import { useResource } from "@/lib/useResource";

/**
 * The agent's record, available from anywhere rather than filed on its own page.
 *
 * It stopped being a destination because nobody navigates to "activity" -- you want it *while*
 * looking at an order or a route, to answer "why does it say that?". A drawer keeps the context
 * you were reading behind it.
 */
export function AgentDrawer({
  open,
  onClose,
  orderId,
}: {
  open: boolean;
  onClose: () => void;
  /** When set, only this order's runs. Used from an order row. */
  orderId?: string;
}) {
  // Fetched for THIS order rather than filtered out of the most recent thirty runs globally. The
  // old version asked for a global page and then filtered it, so an order whose runs had scrolled
  // off the end of that page showed "nothing recorded" while its runs sat in the database.
  const runs = useResource(
    () =>
      !open
        ? Promise.resolve([])
        : orderId
          ? dispatch.conversation(orderId).then((turn) => Object.values(turn.runs))
          : dispatch.agentRuns(30),
    [open, orderId],
  );

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const shown = [...(runs.data ?? [])].sort((a, b) =>
    a.started_at.localeCompare(b.started_at),
  );

  return (
    <>
      <div
        aria-hidden
        onClick={onClose}
        className={cx(
          "fixed inset-0 z-40 bg-ink/20 transition-opacity duration-200",
          open ? "opacity-100" : "pointer-events-none opacity-0",
        )}
      />
      <aside
        role="dialog"
        aria-label="Agent activity"
        aria-hidden={!open}
        className={cx(
          "fixed right-0 top-0 z-50 flex h-screen w-[560px] max-w-[92vw] flex-col",
          "border-l border-rail bg-canvas shadow-[var(--shadow-lift)]",
          "transition-transform duration-250 ease-[var(--ease-out-soft)]",
          open ? "translate-x-0" : "translate-x-full",
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-rail bg-surface px-5 py-4">
          <div className="flex flex-col gap-1">
            <h2 className="font-display text-[20px] leading-none text-ink">Agent activity</h2>
            <p className="max-w-[52ch] text-[12.5px] leading-[1.5] text-ink-muted">
              Every tool the agent called, what it was given and what came back. Operational
              summaries only — no hidden reasoning is recorded or shown.
            </p>
          </div>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {runs.initialising ? (
            <LoadingPanel rows={2} label="Loading agent activity" />
          ) : runs.error ? (
            <ErrorPanel error={runs.error} onRetry={runs.reload} />
          ) : shown.length === 0 ? (
            <EmptyPanel
              title={orderId ? "Nothing recorded for this order" : "The agent hasn't run yet"}
              description="Book a delivery from Customer Chat and the whole sequence appears here."
            />
          ) : (
            <ol className="flex flex-col gap-3">
              {shown.map((run) => (
                <RunCard key={run.id} run={run} />
              ))}
            </ol>
          )}
        </div>

        <footer className="border-t border-rail bg-surface px-5 py-3">
          <p className="font-mono text-[10.5px] leading-[1.5] text-ink-faint">
            Bounded server-side. An action outside the approved list — or outside what this
            customer&apos;s message may do — is refused and logged, never executed.
          </p>
        </footer>
      </aside>
    </>
  );
}
