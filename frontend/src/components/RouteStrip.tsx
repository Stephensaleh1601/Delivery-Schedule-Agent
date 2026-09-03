"use client";

import { useState } from "react";
import { Eyebrow, cx } from "@/components/ui";
import type { DecisionCandidate } from "@/lib/api";

/**
 * Where each candidate lands in the day, as a strip you can read at a glance.
 *
 * "Route-friendly" is an adjective until you can see it. This turns the claim into a picture: the
 * depot, the stops already on that day, and the new one dropped into the position OR-Tools actually
 * chose — with the extra driving attached to the insertion rather than stated in the abstract.
 *
 * A strip rather than a map, deliberately. The Google map on Daily Routes already shows the real
 * geography and needs a live API key; a mid-conversation map that fails to load is a worse demo
 * than no map, and the thing a judge needs here is *where in the sequence*, which a strip says more
 * clearly than pins do. Every number comes from the candidate's own solved evaluation.
 *
 * No other customer is named. Position and count are operational facts about our own day; who is at
 * stop 2 is not this customer's business, and the same component renders beside a WhatsApp thread.
 */
export function RouteStrip({ candidates }: { candidates: DecisionCandidate[] }) {
  const shown = candidates.filter((c) => c.position && c.stops_before !== null);
  const [active, setActive] = useState(0);
  if (shown.length === 0) return null;

  const candidate = shown[Math.min(active, shown.length - 1)];

  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Eyebrow>Where it fits the day</Eyebrow>
        {shown.length > 1 && (
          <div role="tablist" aria-label="Candidate routes" className="flex gap-1">
            {shown.map((c, i) => (
              <button
                key={c.label}
                role="tab"
                aria-selected={i === active}
                onClick={() => setActive(i)}
                className={cx(
                  "rounded-[7px] px-2.5 py-1 text-[11.5px] transition-colors",
                  i === active
                    ? "bg-accent-wash text-accent"
                    : "text-ink-muted hover:bg-sunk hover:text-ink-soft",
                )}
              >
                {c.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <Strip candidate={candidate} />

      {candidate.insertion && (
        <p className="text-[12px] leading-[1.45] text-ink-muted">{candidate.insertion}</p>
      )}
    </section>
  );
}

function Strip({ candidate }: { candidate: DecisionCandidate }) {
  const existing = candidate.stops_before ?? 0;
  // 1-based position among the stops of the PROPOSED day.
  const at = candidate.position ?? existing + 1;

  const nodes: Array<{ key: string; label: string; kind: "depot" | "existing" | "new" }> = [
    { key: "depot-out", label: "Depot", kind: "depot" },
  ];
  let seen = 0;
  for (let i = 1; i <= existing + 1; i++) {
    if (i === at) {
      nodes.push({ key: "new", label: candidate.window, kind: "new" });
    } else {
      seen += 1;
      nodes.push({ key: `stop-${seen}`, label: `Stop ${seen}`, kind: "existing" });
    }
  }
  nodes.push({ key: "depot-back", label: "Depot", kind: "depot" });

  const added = candidate.added_drive_minutes ?? 0;

  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-2 rounded-[10px] border border-rail bg-sunk/40 px-3 py-2.5">
      {nodes.map((node, i) => (
        <div key={node.key} className="flex items-center gap-1">
          {i > 0 && (
            <span
              aria-hidden
              className={cx(
                "text-[11px]",
                // The extra driving is drawn on the legs either side of the new stop, which is
                // where it is actually incurred.
                node.kind === "new" || nodes[i - 1]?.kind === "new"
                  ? "text-pending"
                  : "text-ink-faint",
              )}
            >
              →
            </span>
          )}
          <span
            className={cx(
              "rounded-[7px] px-2 py-1 text-[11.5px] leading-none whitespace-nowrap",
              node.kind === "depot" && "bg-surface text-ink-muted border border-rail",
              node.kind === "existing" && "bg-surface text-ink-soft border border-rail",
              node.kind === "new" &&
                "bg-locked-wash text-locked border border-locked-edge font-medium",
            )}
          >
            {node.kind === "new" ? `NEW · ${node.label}` : node.label}
          </span>
        </div>
      ))}
      <span
        className={cx(
          "ml-auto font-mono text-[11.5px] tnum",
          added > 15 ? "text-pending" : "text-ink-muted",
        )}
      >
        {added > 0 ? `+${added}m driving` : added < 0 ? `−${Math.abs(added)}m driving` : "no extra driving"}
      </span>
    </div>
  );
}
