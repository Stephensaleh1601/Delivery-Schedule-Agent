"use client";

import { Pill, cx } from "@/components/ui";
import type { Evaluation } from "@/lib/api";
import { formatDuration } from "@/lib/format";

/**
 * What saying yes to a slot would do to the day.
 *
 * Never a single number. "Cost: 93" tells a reader nothing they can check or argue with, and the
 * interesting case in this product is precisely when the cheapest slot is NOT the customer's first
 * choice -- which is only persuasive if you can see why.
 *
 * Route efficiency is the objective and is laid out as a before/after ledger. Customer preference
 * is a courtesy weight, so it sits below a rule, quieter, and is labelled as a preference rather
 * than as a cost of driving.
 */

export function RouteImpactTable({
  evaluation,
  recommended = false,
}: {
  evaluation: Evaluation;
  recommended?: boolean;
}) {
  if (!evaluation.feasible) {
    return (
      <div className="rounded-[10px] border border-alert-edge bg-alert-wash px-4 py-3">
        <div className="flex items-center gap-2">
          <Pill tone="alert">Can&apos;t be served</Pill>
        </div>
        <p className="mt-1.5 text-[12.5px] leading-[1.5] text-ink-soft">
          {evaluation.infeasible_reason}
        </p>
      </div>
    );
  }

  const impact = evaluation.route_impact;
  const driveDelta = impact.drive_minutes.after - impact.drive_minutes.before;
  const preferencePenalty = evaluation.breakdown.preference_penalty_minutes;

  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-[minmax(0,1fr)_78px_16px_78px_62px] items-baseline gap-x-2">
        <Header>Route efficiency</Header>
        <Header align="right">before</Header>
        <span />
        <Header align="right">after</Header>
        <Header align="right">change</Header>

        <Row
          label="Driving"
          before={formatDuration(impact.drive_minutes.before)}
          after={formatDuration(impact.drive_minutes.after)}
          delta={driveDelta === 0 ? "—" : `${driveDelta > 0 ? "+" : "−"}${formatDuration(Math.abs(driveDelta))}`}
          tone={driveDelta > 0 ? "cost" : "neutral"}
          emphasise
        />
        <Row
          label="Stops"
          before={String(impact.stops.before)}
          after={String(impact.stops.after)}
          delta={`+${impact.stops.after - impact.stops.before}`}
        />
        <Row
          label="Day finishes"
          before={impact.finishes_at.before ?? "—"}
          after={impact.finishes_at.after}
          delta={impact.finishes_at.before ? "" : "new day"}
          tone={impact.finishes_at.before ? "neutral" : "cost"}
        />
        {impact.opens_empty_day && (
          <Row
            label="Opens an empty day"
            before="no work"
            after="1 stop"
            delta={`+${impact.empty_day_overhead_minutes}m`}
            tone="cost"
          />
        )}
      </div>

      <div className="flex items-baseline justify-between gap-3 border-t border-rail pt-2">
        <span className="text-[12.5px] text-ink-muted">
          Customer preference
          <span className="ml-1.5 font-mono text-[11px] text-ink-faint">
            {ordinal(impact.preference_rank)} choice
          </span>
        </span>
        <span
          className={cx(
            "font-mono text-[12.5px] tnum",
            preferencePenalty > 0 ? "text-pending" : "text-ink-faint",
          )}
        >
          {preferencePenalty > 0 ? `+${preferencePenalty}m` : "—"}
        </span>
      </div>

      <div className="flex items-baseline justify-between gap-3 rounded-[8px] bg-sunk/70 px-3 py-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
          Route impact
        </span>
        <span
          className={cx(
            "font-display text-[22px] leading-none tnum",
            recommended ? "text-locked" : "text-ink",
          )}
        >
          {evaluation.total_score}
          <span className="ml-1 font-sans text-[11px] text-ink-faint">min</span>
        </span>
      </div>
    </div>
  );
}

/** The compact form: one line per candidate, for a list. */
export function RouteImpactSummary({ evaluation }: { evaluation: Evaluation }) {
  if (!evaluation.feasible) {
    return <span className="text-[12px] text-alert">can&apos;t be served</span>;
  }
  const impact = evaluation.route_impact;
  const delta = impact.drive_minutes.after - impact.drive_minutes.before;
  return (
    <span className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 text-[12px] text-ink-muted">
      <span className="font-mono text-ink-soft tnum">
        {delta >= 0 ? "+" : "−"}
        {formatDuration(Math.abs(delta))} driving
      </span>
      <span>·</span>
      <span className="tnum">
        {impact.stops.before} → {impact.stops.after} stops
      </span>
      {impact.opens_empty_day && (
        <>
          <span>·</span>
          <span className="text-pending">opens an empty day</span>
        </>
      )}
    </span>
  );
}

/** Before/after for a whole published day, used when a booking has just landed on it. */
export function DayChange({
  before,
  after,
  label = "This day",
}: {
  before: { drive_minutes: number; stops: number; distance_km: number; finishes_at: string | null } | null;
  after: { drive_minutes: number; stops: number; distance_km: number; finishes_at: string | null };
  label?: string;
}) {
  const rows = [
    {
      key: "Driving",
      before: before ? formatDuration(before.drive_minutes) : "—",
      after: formatDuration(after.drive_minutes),
      delta: before ? after.drive_minutes - before.drive_minutes : null,
      unit: "m",
    },
    {
      key: "Distance",
      before: before ? `${before.distance_km} km` : "—",
      after: `${after.distance_km} km`,
      delta: before ? Number((after.distance_km - before.distance_km).toFixed(1)) : null,
      unit: " km",
    },
    {
      key: "Stops",
      before: before ? String(before.stops) : "—",
      after: String(after.stops),
      delta: before ? after.stops - before.stops : null,
      unit: "",
    },
    {
      key: "Day finishes",
      before: before?.finishes_at ?? "—",
      after: after.finishes_at ?? "—",
      delta: null,
      unit: "",
    },
  ];

  return (
    <div className="flex flex-col gap-1.5">
      {/* Five columns: Row emits label, before, arrow, after, delta. */}
      <div className="grid grid-cols-[minmax(0,1fr)_74px_16px_74px_66px] items-baseline gap-x-2">
        <Header>{label}</Header>
        <Header align="right">before</Header>
        <span />
        <Header align="right">after</Header>
        <Header align="right">change</Header>
        {rows.map((row) => (
          <Row
            key={row.key}
            label={row.key}
            before={row.before}
            after={row.after}
            delta={
              row.delta === null || row.delta === 0
                ? "—"
                : `${row.delta > 0 ? "+" : "−"}${Math.abs(row.delta)}${row.unit}`
            }
            tone={row.delta !== null && row.delta > 0 ? "cost" : "neutral"}
            emphasise={row.key === "Driving"}
          />
        ))}
      </div>
    </div>
  );
}

// -- bits ---------------------------------------------------------------------

function Header({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "right" }) {
  return (
    <span
      className={cx(
        "font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint",
        align === "right" && "text-right",
      )}
    >
      {children}
    </span>
  );
}

function Row({
  label,
  before,
  after,
  delta,
  tone = "neutral",
  emphasise = false,
}: {
  label: string;
  before: string;
  after: string;
  delta: string;
  tone?: "neutral" | "cost";
  emphasise?: boolean;
}) {
  return (
    <>
      <span
        className={cx(
          "col-start-1 py-[3px] text-[12.5px]",
          emphasise ? "font-medium text-ink" : "text-ink-soft",
        )}
      >
        {label}
      </span>
      <span className="py-[3px] text-right font-mono text-[12.5px] text-ink-muted tnum">
        {before}
      </span>
      <span className="py-[3px] text-center text-[11px] text-ink-faint">→</span>
      <span
        className={cx(
          "py-[3px] text-right font-mono text-[12.5px] tnum",
          emphasise ? "font-medium text-ink" : "text-ink-soft",
        )}
      >
        {after}
      </span>
      <span
        className={cx(
          "py-[3px] text-right font-mono text-[12px] tnum",
          tone === "cost" ? "text-pending" : "text-ink-faint",
        )}
      >
        {delta}
      </span>
    </>
  );
}

function ordinal(n: number): string {
  return ["", "1st", "2nd", "3rd"][n] ?? `${n}th`;
}
