"use client";

import Link from "next/link";
import { Page } from "@/components/Shell";
import {
  Button,
  Card,
  Eyebrow,
  EmptyPanel,
  ErrorPanel,
  LoadingPanel,
  LockIcon,
  Pill,
  SectionHeader,
  Skeleton,
  Stat,
  cx,
  statusTone,
} from "@/components/ui";
import { dispatch, type Order, type PlanVersion } from "@/lib/api";
import { STATUS_LABELS, dayParts, formatDuration, formatRelative } from "@/lib/format";
import { useResource } from "@/lib/useResource";

export default function OverviewPage() {
  const horizon = useResource(() => dispatch.horizon(), []);
  const orders = useResource(() => dispatch.orders(), []);
  const metrics = useResource(() => dispatch.metrics(), []);
  const exceptions = useResource(() => dispatch.exceptions(), []);
  const runs = useResource(() => dispatch.agentRuns(4), []);

  // One unreachable backend should say so once, not six times down the page.
  const fatal = horizon.error ?? orders.error ?? metrics.error;
  if (fatal) {
    return (
      <Page title="Operations" lede="Live state across the bookable horizon.">
        <ErrorPanel
          error={fatal}
          onRetry={() => {
            horizon.reload();
            orders.reload();
            metrics.reload();
          }}
        />
      </Page>
    );
  }

  const all = orders.data ?? [];
  const byStatus = (...statuses: string[]) =>
    all.filter((o) => statuses.includes(o.planning_status));

  const awaiting = byStatus("pending_planning", "pending_availability");
  const offered = byStatus("offered");
  const committed = byStatus("confirmed", "sequenced", "dispatched");
  const delayed = all.filter((o) => o.readiness_status === "delayed");

  return (
    <Page
      title="Operations"
      lede="Every figure here is read from the dispatch service — the plans, the promises and the agent's own record of what it did."
      actions={
        horizon.data ? (
          <Pill tone="neutral">Today {horizon.data.today}</Pill>
        ) : (
          <Skeleton className="h-6 w-32" />
        )
      }
    >
      {/* -- what the agent protected ------------------------------------- */}
      <section className="enter flex flex-col gap-3.5" style={{ animationDelay: "40ms" }}>
        <SectionHeader
          eyebrow="Measured impact"
          title="Computed from stored plans, not asserted"
          description="Appointments moved is a real count: every published stop compared against the window that customer was promised."
        />
        {metrics.initialising ? (
          <div className="grid grid-cols-5 gap-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-[104px]" />
            ))}
          </div>
        ) : metrics.data ? (
          <div className="grid grid-cols-5 gap-3">
            <Stat
              label="Customers moved"
              value={metrics.data.confirmed_appointments_moved}
              note="Confirmed appointments shifted without consent."
              tone={metrics.data.confirmed_appointments_moved === 0 ? "locked" : "alert"}
              emphasis
            />
            <Stat
              label="Stops planned"
              value={metrics.data.scheduled_stops}
              note={`Across ${metrics.data.horizon.first} – ${metrics.data.horizon.last}.`}
            />
            <Stat
              label="Drive time"
              value={formatDuration(metrics.data.round_trip_drive_minutes)}
              note="Round trip, including the drive home."
            />
            <Stat
              label="Plan versions"
              value={metrics.data.plan_versions}
              note="History kept; no-op replans don't count."
            />
            <Stat
              label="Interventions"
              value={metrics.data.coordinator_interventions}
              note="Escalated rather than guessed."
              tone={metrics.data.coordinator_interventions > 0 ? "pending" : "neutral"}
            />
          </div>
        ) : null}
      </section>

      {/* -- the horizon --------------------------------------------------- */}
      <section className="enter flex flex-col gap-3.5" style={{ animationDelay: "90ms" }}>
        <SectionHeader
          eyebrow="Planning horizon"
          title="The four days a customer can book into"
          description="A new order is priced against each of these. An empty day costs an hour of penalty to open, which is why they consolidate."
        />
        {horizon.initialising ? (
          <Skeleton className="h-[124px]" />
        ) : (
          <div className="grid grid-cols-4 gap-3">
            {(horizon.data?.dates ?? []).map((date, i) => (
              <HorizonDay key={date} date={date} orders={all} index={i} />
            ))}
          </div>
        )}
      </section>

      {/* -- pipeline ------------------------------------------------------ */}
      <section className="enter flex flex-col gap-3.5" style={{ animationDelay: "140ms" }}>
        <SectionHeader
          eyebrow="Order pipeline"
          title="Where every order stands"
          actions={
            <Link href="/planning">
              <Button variant="secondary">Plan an order</Button>
            </Link>
          }
        />
        {orders.initialising ? (
          <LoadingPanel rows={2} label="Loading orders" />
        ) : all.length === 0 ? (
          <EmptyPanel
            title="No orders yet"
            description="Seed the demo database to populate the horizon."
          />
        ) : (
          <div className="grid grid-cols-4 gap-3">
            <PipelineColumn
              label="Awaiting planning"
              tone="pending"
              orders={awaiting}
              note="No date promised yet."
            />
            <PipelineColumn
              label="Offer sent"
              tone="accent"
              orders={offered}
              note="Waiting on the customer."
            />
            <PipelineColumn
              label="Committed"
              tone="locked"
              orders={committed}
              note="Window locked and protected."
              locked
            />
            <PipelineColumn
              label="Goods delayed"
              tone="alert"
              orders={delayed}
              note="Off the route, slot recoverable."
              emptyText="Nothing delayed"
            />
          </div>
        )}
      </section>

      {/* -- attention + activity ------------------------------------------ */}
      <div className="enter grid grid-cols-2 gap-5" style={{ animationDelay: "190ms" }}>
        <section className="flex flex-col gap-3.5">
          <SectionHeader eyebrow="Needs a human" title="Coordinator escalations" />
          {exceptions.initialising ? (
            <Skeleton className="h-24" />
          ) : (exceptions.data ?? []).length === 0 ? (
            <EmptyPanel title="Nothing escalated" description="The agent has resolved everything it was asked to." />
          ) : (
            <ul className="flex flex-col gap-2">
              {(exceptions.data ?? []).slice(0, 4).map((e) => (
                <Card key={e.id} as="li" settled tone="alert" className="px-4 py-3">
                  <div className="flex flex-col gap-1">
                    <div className="flex items-center gap-2">
                      <Pill tone="alert">{e.kind.replace(/_/g, " ")}</Pill>
                      {e.delivery_date && (
                        <span className="font-mono text-[11px] text-ink-faint">{e.delivery_date}</span>
                      )}
                    </div>
                    <p className="text-[13px] leading-[1.5] text-ink-soft">{e.message}</p>
                  </div>
                </Card>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3.5">
          <SectionHeader
            eyebrow="Agent activity"
            title="What it actually did"
            actions={
              <Link href="/activity">
                <Button variant="ghost">All runs →</Button>
              </Link>
            }
          />
          {runs.initialising ? (
            <Skeleton className="h-24" />
          ) : (runs.data ?? []).length === 0 ? (
            <EmptyPanel
              title="No agent runs yet"
              description="Book an order from the Conversation screen to see the loop work."
            />
          ) : (
            <ul className="flex flex-col gap-2">
              {(runs.data ?? []).map((run) => (
                <Card key={run.id} as="li" className="px-4 py-3">
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-[13px] font-medium text-ink">
                        {(run.event_type ?? "event").replace(/_/g, " ")}
                      </span>
                      <span className="font-mono text-[11px] text-ink-faint">
                        {formatRelative(run.started_at)}
                      </span>
                    </div>
                    <p className="line-clamp-2 text-[12.5px] leading-[1.5] text-ink-muted">
                      {run.final_summary || "—"}
                    </p>
                    <div className="flex gap-1">
                      {run.actions.map((a) => (
                        <span
                          key={a.step}
                          title={`${a.tool}: ${a.summary}`}
                          className={cx(
                            "h-1 flex-1 rounded-full",
                            a.ok ? "bg-locked/45" : "bg-alert/55",
                          )}
                        />
                      ))}
                    </div>
                  </div>
                </Card>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Page>
  );
}

/** One day in the horizon strip. Load is shown as a filled bar rather than a number alone, so
 *  the empty day is visible at a glance -- that is the whole point of the penalty. */
function HorizonDay({ date, orders, index }: { date: string; orders: Order[]; index: number }) {
  const { weekday, day, month } = dayParts(date);
  const onDay = orders.filter((o) => o.delivery_date === date);
  const routable = onDay.filter(
    (o) =>
      ["confirmed", "sequenced", "dispatched"].includes(o.planning_status) &&
      o.readiness_status === "ready",
  );
  const delayed = onDay.filter((o) => o.readiness_status === "delayed");
  const empty = routable.length === 0;

  return (
    <Card
      settled={!empty}
      tone={empty ? "pending" : "locked"}
      className={cx("px-4 py-3.5", empty && "border-dashed")}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col">
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
            {weekday} · N+{index + 2}
          </span>
          <span className="font-display text-[26px] leading-[1.1] text-ink">
            {day} <span className="text-[16px] text-ink-muted">{month}</span>
          </span>
        </div>
        {empty ? (
          <Pill tone="pending">Empty</Pill>
        ) : (
          <Pill tone="locked" icon={<LockIcon />}>
            {routable.length}
          </Pill>
        )}
      </div>

      <div className="mt-3 flex gap-[3px]" aria-hidden>
        {Array.from({ length: 6 }).map((_, i) => (
          <span
            key={i}
            className={cx(
              "h-1.5 flex-1 rounded-full",
              i < routable.length ? "bg-locked/55" : "bg-rail",
            )}
          />
        ))}
      </div>

      <p className="mt-2 text-[11.5px] leading-[1.45] text-ink-muted">
        {empty
          ? "Opening this day costs a 60-minute penalty."
          : `${routable.length} stop${routable.length === 1 ? "" : "s"} committed`}
        {delayed.length > 0 && (
          <span className="text-alert"> · {delayed.length} delayed</span>
        )}
      </p>
    </Card>
  );
}

function PipelineColumn({
  label,
  tone,
  orders,
  note,
  locked = false,
  emptyText = "None",
}: {
  label: string;
  tone: "pending" | "accent" | "locked" | "alert";
  orders: Order[];
  note: string;
  locked?: boolean;
  emptyText?: string;
}) {
  return (
    <Card className="flex flex-col gap-2.5 px-4 py-3.5">
      <div className="flex items-baseline justify-between gap-2">
        <Eyebrow>{label}</Eyebrow>
        <span className="font-display text-[22px] leading-none text-ink tnum">{orders.length}</span>
      </div>
      <p className="text-[11.5px] leading-[1.4] text-ink-faint">{note}</p>
      <ul className="flex flex-col gap-1 border-t border-rail pt-2.5">
        {orders.length === 0 ? (
          <li className="text-[12px] text-ink-faint">{emptyText}</li>
        ) : (
          orders.slice(0, 4).map((o) => (
            <li key={o.id} className="flex items-center justify-between gap-2">
              <span className="truncate text-[12.5px] text-ink-soft">{o.customer_name}</span>
              {locked && o.locked_window ? (
                <span className="shrink-0 font-mono text-[10.5px] text-locked">
                  {o.locked_window.start}
                </span>
              ) : (
                <span className="shrink-0 font-mono text-[10.5px] text-ink-faint">
                  {o.availability_options.length || "—"}
                </span>
              )}
            </li>
          ))
        )}
        {orders.length > 4 && (
          <li className="pt-0.5 text-[11.5px] text-ink-faint">+{orders.length - 4} more</li>
        )}
      </ul>
      <span className="sr-only">
        {orders.map((o) => `${o.customer_name}: ${STATUS_LABELS[o.planning_status]}`).join(", ")}
      </span>
    </Card>
  );
}
