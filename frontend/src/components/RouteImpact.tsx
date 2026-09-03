"use client";

import { Pill, cx } from "@/components/ui";
import type { Evaluation } from "@/lib/api";
import { formatDuration } from "@/lib/format";

/**
 * What saying yes to a slot would do to the day.
 *
 * Never a single number, and specifically never `total_score`. That figure is a ranking index: it
 * folds a 60-minute empty-day penalty -- a planning weight nobody drives -- in with real driving
 * minutes, so printing it as "Route impact: 93 min" states a duration that does not exist. Anyone
 * reading it would be right to check it against the map and wrong about what they found.
 *
 * So every component is shown separately with its own unit: driving in minutes, distance in
 * kilometres, opening a delivery day as a yes/no, overtime as the real minutes past the soft end.
 * Customer preference sits below a rule, unpriced, because it is a fact about the customer rather
 * than a cost of travel.
 */

export function RouteImpactTable({ evaluation }: { evaluation: Evaluation }) {
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
  const distanceDelta = Number((impact.distance_km.after - impact.distance_km.before).toFixed(1));

  return (
    <div className="flex flex-col gap-2">
      {impact.coordinator_reason && (
        // Read off the solved sequence by planning/route_facts.py -- no model wrote this, which is
        // why it can name where the van is and who it sits between without inventing either.
        <p className="text-[12.5px] leading-[1.45] text-ink-soft">{impact.coordinator_reason}</p>
      )}
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
          label="Distance"
          before={`${impact.distance_km.before} km`}
          after={`${impact.distance_km.after} km`}
          delta={
            distanceDelta === 0
              ? "—"
              : `${distanceDelta > 0 ? "+" : "−"}${Math.abs(distanceDelta).toFixed(1)} km`
          }
          tone={distanceDelta > 0 ? "cost" : "neutral"}
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
        {impact.overtime_minutes > 0 && (
          <Row
            label="Overtime"
            before="—"
            after={formatDuration(impact.overtime_minutes)}
            delta={`+${formatDuration(impact.overtime_minutes)}`}
            tone="cost"
          />
        )}
        {/* A yes/no, never "+60m": opening a day is a decision we weight for ranking, not an hour
            anyone spends behind a wheel. */}
        <Row
          label="Opens a new delivery day"
          before={impact.opens_empty_day ? "no work" : "already running"}
          after={impact.opens_empty_day ? "yes" : "no"}
          delta={impact.opens_empty_day ? "yes" : "—"}
          tone={impact.opens_empty_day ? "cost" : "neutral"}
        />
      </div>

      <div className="flex items-baseline justify-between gap-3 border-t border-rail pt-2">
        <span className="text-[12.5px] text-ink-muted">Customer preference</span>
        {/* Unpriced on purpose. Preference breaks ties between operationally comparable days; a
            minutes figure beside real driving time invites reading it as a cost of travel. */}
        <span className="text-[12.5px] text-ink-soft">
          {ordinal(impact.preference_rank)} choice
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

export interface DaySummary {
  drive_minutes: number;
  stops: number;
  distance_km: number;
  finishes_at: string | null;
  /** Minutes since midnight, so "Day finishes" can show a real delta rather than an em-dash. */
  completion_minutes?: number | null;
  /** Door to door, and how much of it is spent waiting. A "+1 driving minute" headline must not be
   *  able to hide a multi-hour increase in the crew's day. */
  working_span_minutes?: number;
  idle_minutes?: number;
}

/** Before/after for a whole published day, used when a booking has just landed on it. */
export function DayChange({
  before,
  after,
  label = "This day",
}: {
  before: DaySummary | null;
  after: DaySummary;
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
      // A real delta, from the minutes. This printed an em-dash because two "17:34"-shaped strings
      // cannot be subtracted -- so a route that finished nearly four hours later looked unchanged
      // next to a "+1 minute" driving row.
      delta:
        before?.completion_minutes != null && after.completion_minutes != null
          ? after.completion_minutes - before.completion_minutes
          : null,
      unit: "m",
    },
    {
      key: "Crew waiting",
      before: before ? formatDuration(before.idle_minutes ?? 0) : "—",
      after: formatDuration(after.idle_minutes ?? 0),
      delta: before ? (after.idle_minutes ?? 0) - (before.idle_minutes ?? 0) : null,
      unit: "m",
    },
    {
      key: "Crew's day",
      before: before ? formatDuration(before.working_span_minutes ?? 0) : "—",
      after: formatDuration(after.working_span_minutes ?? 0),
      delta: before
        ? (after.working_span_minutes ?? 0) - (before.working_span_minutes ?? 0)
        : null,
      unit: "m",
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
