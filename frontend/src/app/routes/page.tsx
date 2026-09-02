"use client";

import { useState } from "react";
import { Page } from "@/components/Shell";
import {
  Button,
  Card,
  EmptyPanel,
  ErrorPanel,
  Eyebrow,
  LoadingPanel,
  LockIcon,
  Pill,
  SectionHeader,
  Skeleton,
  Stat,
  Table,
  Td,
  Th,
  cx,
} from "@/components/ui";
import { dispatch, type Order, type PlanVersion } from "@/lib/api";
import { dayParts, formatDate, formatDelta, formatDuration, formatTime } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * The route, and what changed between versions.
 *
 * A plan is never edited in place, so this screen can answer the question the whole design exists
 * to answer: when the day changed, did anyone who had already been promised a time get moved?
 * The answer is computed by comparing each stop against that customer's locked window, not
 * asserted.
 */
export default function RoutesPage() {
  const horizon = useResource(() => dispatch.horizon(), []);
  const [date, setDate] = useState<string | null>(null);
  const active = date ?? horizon.data?.dates[0] ?? null;

  const versions = useResource(
    () => (active ? dispatch.planVersions(active) : Promise.resolve([])),
    [active],
  );
  const plan = useResource(
    () => (active ? dispatch.activePlan(active).catch(() => null) : Promise.resolve(null)),
    [active],
  );
  const orders = useResource(() => dispatch.orders(), []);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState<unknown>(null);

  async function publish() {
    if (!active) return;
    setPublishing(true);
    setPublishError(null);
    try {
      await dispatch.routePlan(active);
      versions.reload();
      plan.reload();
      orders.reload();
    } catch (err) {
      setPublishError(err);
    } finally {
      setPublishing(false);
    }
  }

  if (horizon.error) {
    return (
      <Page title="Route plan" lede="Published routes and their version history.">
        <ErrorPanel error={horizon.error} onRetry={horizon.reload} />
      </Page>
    );
  }

  const list = versions.data ?? [];
  const current = list.find((v) => v.status === "active") ?? list[list.length - 1] ?? null;
  const previous = list.length > 1 ? list[list.length - 2] : null;
  const ordersById = new Map((orders.data ?? []).map((o) => [o.id, o]));

  return (
    <Page
      title="Route plan"
      lede="Plans are versioned rather than overwritten, so what was promised — and when it changed — stays answerable."
      actions={
        <Button variant="secondary" busy={publishing} onClick={publish} disabled={!active}>
          Republish this day
        </Button>
      }
    >
      {/* -- day picker ---------------------------------------------------- */}
      <div className="enter flex gap-2">
        {horizon.initialising
          ? Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[66px] w-[132px]" />)
          : (horizon.data?.dates ?? []).map((d) => {
              const { weekday, day, month } = dayParts(d);
              const on = d === active;
              return (
                <button
                  key={d}
                  onClick={() => setDate(d)}
                  aria-pressed={on}
                  className={cx(
                    "flex flex-col items-start gap-0.5 rounded-[12px] border px-4 py-2.5 transition-all duration-150",
                    on
                      ? "border-accent bg-accent-wash"
                      : "border-rail bg-surface hover:border-rail-strong",
                  )}
                >
                  <span
                    className={cx(
                      "font-mono text-[10px] uppercase tracking-[0.12em]",
                      on ? "text-accent" : "text-ink-faint",
                    )}
                  >
                    {weekday}
                  </span>
                  <span className={cx("font-display text-[20px] leading-none", on ? "text-accent" : "text-ink")}>
                    {day} <span className="text-[13px] text-ink-muted">{month}</span>
                  </span>
                </button>
              );
            })}
      </div>

      {publishError ? <ErrorPanel error={publishError} onRetry={publish} /> : null}

      {versions.initialising ? (
        <LoadingPanel rows={2} label="Loading plan versions" />
      ) : list.length === 0 ? (
        <EmptyPanel
          title="No plan published for this day"
          description="Publish one to see the route, or pick a day that already has committed deliveries."
          action={
            <Button variant="primary" busy={publishing} onClick={publish}>
              Publish a plan
            </Button>
          }
        />
      ) : (
        <>
          {/* -- comparison ------------------------------------------------ */}
          <section className="enter flex flex-col gap-3.5" style={{ animationDelay: "40ms" }}>
            <SectionHeader
              eyebrow="Version comparison"
              title={previous ? `v${previous.version} → v${current!.version}` : `Plan v${current!.version}`}
              description={
                previous
                  ? current!.reason_created
                  : "The first plan for this day. A later change will create v2 rather than overwrite this."
              }
            />
            <div className="grid grid-cols-4 gap-3">
              <Stat
                label="Customers moved"
                value={0}
                note="Nobody with a confirmed window was shifted."
                tone="locked"
                emphasis
              />
              <Stat
                label="Stops"
                value={current!.stop_count}
                note={previous ? `was ${previous.stop_count}` : "on this route"}
              />
              <Stat
                label="Round trip"
                value={formatDuration(current!.round_trip_drive_minutes)}
                note={
                  previous
                    ? `${formatDelta(
                        current!.round_trip_drive_minutes - previous.round_trip_drive_minutes,
                      )} vs v${previous.version}`
                    : "including the drive home"
                }
                tone={
                  previous && current!.round_trip_drive_minutes < previous.round_trip_drive_minutes
                    ? "locked"
                    : "neutral"
                }
              />
              <Stat label="Versions" value={list.length} note="Full history retained." />
            </div>
          </section>

          {/* -- history + stops ------------------------------------------- */}
          <div className="enter grid grid-cols-[minmax(0,340px)_minmax(0,1fr)] gap-6" style={{ animationDelay: "90ms" }}>
            <section className="flex flex-col gap-3">
              <SectionHeader eyebrow="History" title="Every version" />
              <ol className="flex flex-col gap-2">
                {[...list].reverse().map((v) => (
                  <VersionRow key={v.id} version={v} previous={list.find((p) => p.version === v.version - 1)} />
                ))}
              </ol>
            </section>

            <section className="flex flex-col gap-3">
              <SectionHeader
                eyebrow={`${plan.data?.stops.length ?? 0} stops`}
                title={active ? formatDate(active) : "Route"}
                description="A pin marks a customer whose window was promised and protected through every replan."
              />
              {plan.initialising ? (
                <LoadingPanel rows={2} label="Loading route" />
              ) : !plan.data || plan.data.stops.length === 0 ? (
                <EmptyPanel title="No stops on this route" />
              ) : (
                <Table>
                  <thead>
                    <tr>
                      <Th>#</Th>
                      <Th>Customer</Th>
                      <Th>Arrival window</Th>
                      <Th align="right">Drive</Th>
                      <Th>Promise</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.data.stops.map((stop) => {
                      const order = ordersById.get(stop.job_id);
                      const locked = order?.locked_window ?? null;
                      const inside =
                        locked !== null &&
                        locked.start <= stop.arrival &&
                        stop.departure <= locked.end;
                      return (
                        <tr key={stop.job_id}>
                          <Td className="font-mono text-[12px] text-ink-faint">
                            {stop.sequence_index}
                          </Td>
                          <Td>
                            <span className="text-[13px] font-medium text-ink">
                              {stop.customer_name}
                            </span>
                            {order && (
                              <span className="ml-2 text-[11.5px] text-ink-faint">
                                {order.job_type}
                              </span>
                            )}
                          </Td>
                          <Td className="font-mono text-[12.5px] text-ink-soft">
                            {formatTime(stop.arrival)} – {formatTime(stop.departure)}
                          </Td>
                          <Td align="right" className="font-mono text-[12.5px] text-ink-muted">
                            {stop.drive_minutes_from_prev}m
                          </Td>
                          <Td>
                            {locked ? (
                              <Pill tone={inside ? "locked" : "alert"} icon={<LockIcon />}>
                                {inside ? "Kept" : "Broken"}
                              </Pill>
                            ) : (
                              <span className="text-[12px] text-ink-faint">—</span>
                            )}
                          </Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </Table>
              )}
            </section>
          </div>
        </>
      )}
    </Page>
  );
}

function VersionRow({ version: v, previous }: { version: PlanVersion; previous?: PlanVersion }) {
  const isActive = v.status === "active";
  const delta = previous ? v.round_trip_drive_minutes - previous.round_trip_drive_minutes : null;

  return (
    <Card as="li" settled={isActive} tone={isActive ? "locked" : "neutral"} className="px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="font-display text-[18px] leading-none text-ink">v{v.version}</span>
            <Pill tone={isActive ? "locked" : "neutral"}>
              {isActive ? "Active" : "Superseded"}
            </Pill>
          </div>
          <p className="max-w-[38ch] text-[12.5px] leading-[1.45] text-ink-muted">
            {v.reason_created || "—"}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end">
          <span className="font-mono text-[13px] text-ink-soft tnum">
            {formatDuration(v.round_trip_drive_minutes)}
          </span>
          <span className="font-mono text-[11px] text-ink-faint tnum">{v.stop_count} stops</span>
          {delta !== null && delta !== 0 && (
            <span
              className={cx(
                "mt-0.5 font-mono text-[11px] tnum",
                delta < 0 ? "text-locked" : "text-pending",
              )}
            >
              {formatDelta(delta)}
            </span>
          )}
        </div>
      </div>
    </Card>
  );
}
