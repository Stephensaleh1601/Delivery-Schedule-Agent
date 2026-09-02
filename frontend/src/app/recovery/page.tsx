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
  Stat,
  cx,
} from "@/components/ui";
import { dispatch, type Order, type ReadinessResponse } from "@/lib/api";
import { formatDate, formatDateShort, formatDuration, formatWindow } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * What happens when the goods don't turn up.
 *
 * The point of this screen is that a delay costs two things -- the delivery AND the slot -- and
 * only the second one is recoverable. So it shows the freed capacity as a thing with a value,
 * and the customers who could take it, ranked on exactly the same terms as a fresh booking.
 *
 * Consent is visible throughout: only customers who said they'd take an earlier delivery appear
 * as candidates, and being a candidate is an offer, never a move.
 */
export default function RecoveryPage() {
  const orders = useResource(() => dispatch.orders(), []);
  const [outcome, setOutcome] = useState<ReadinessResponse | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [morning, setMorning] = useState<{ date: string; dispatched: number } | null>(null);

  const all = orders.data ?? [];
  const scheduled = all.filter(
    (o) => ["confirmed", "sequenced", "dispatched"].includes(o.planning_status) && o.delivery_date,
  );
  const ready = scheduled.filter((o) => o.readiness_status === "ready");
  const delayed = scheduled.filter((o) => o.readiness_status === "delayed");
  const flexible = ready.filter((o) => o.can_deliver_early);

  async function setReadiness(order: Order, readiness: "ready" | "delayed") {
    setBusyId(order.id);
    setError(null);
    try {
      const result = await dispatch.setReadiness(order.id, readiness);
      setOutcome(readiness === "delayed" ? result : null);
      orders.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusyId(null);
    }
  }

  async function runMorning(date: string) {
    setBusyId("morning");
    setError(null);
    try {
      const result = await dispatch.morningRun(date);
      if (result.error) setError(new Error(result.error));
      else setMorning({ date: result.date, dispatched: result.dispatched });
      orders.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusyId(null);
    }
  }

  if (orders.error) {
    return (
      <Page title="Delays & recovery" lede="When goods don't arrive, the slot is still worth something.">
        <ErrorPanel error={orders.error} onRetry={orders.reload} />
      </Page>
    );
  }

  return (
    <Page
      title="Delays & recovery"
      lede="A delay loses the delivery and wastes the slot. Only the second is recoverable — and only by asking someone who already agreed to come forward."
      actions={
        outcome?.freed_date ? (
          <Button
            variant="secondary"
            busy={busyId === "morning"}
            onClick={() => runMorning(outcome.freed_date!)}
          >
            Simulate morning run
          </Button>
        ) : null
      }
    >
      {error ? <ErrorPanel error={error} onRetry={() => setError(null)} /> : null}

      {morning && (
        <Card tone="accent" className="enter flex items-center justify-between gap-4 px-5 py-3.5">
          <div className="flex flex-col">
            <Eyebrow>Morning run</Eyebrow>
            <span className="text-[13.5px] text-ink-soft">
              {formatDate(morning.date)} dispatched — {morning.dispatched} stop
              {morning.dispatched === 1 ? "" : "s"}, each customer sent a reminder.
            </span>
          </div>
          <Button variant="ghost" onClick={() => setMorning(null)}>
            Dismiss
          </Button>
        </Card>
      )}

      {/* -- recovery outcome ---------------------------------------------- */}
      {outcome && (
        <section className="enter flex flex-col gap-3.5">
          <SectionHeader
            eyebrow="Slot freed"
            title={`${formatDate(outcome.freed_date!)} — the day has been rebuilt`}
            description="The delayed order kept its history; it simply stopped being routable. Everyone else's promised window was verified before the new plan was published."
          />
          <div className="grid grid-cols-4 gap-3">
            <Stat
              label="Customers moved"
              value={0}
              note="Removing a stop reshuffles the day. Nobody promised a time was shifted."
              tone="locked"
              emphasis
            />
            <Stat
              label="New plan"
              value={outcome.plan_version ? `v${outcome.plan_version}` : "—"}
              note="Published; the previous version stays readable."
            />
            <Stat
              label="Candidates"
              value={outcome.replacements.length}
              note="Customers who agreed to an earlier delivery."
              tone={outcome.replacements.length > 0 ? "accent" : "pending"}
            />
            <Stat
              label="Offer rounds left"
              value={2}
              note="Capped, so a delay can't become a phone-around."
            />
          </div>

          {outcome.replacements.length === 0 ? (
            <EmptyPanel
              title="Nobody to move forward"
              description="No customer on a later day has agreed to an earlier delivery, so this slot stays idle. The back office has been notified."
            />
          ) : (
            <ol className="grid grid-cols-2 gap-3">
              {outcome.replacements.map((r, i) => (
                <Card
                  key={r.order_id}
                  as="li"
                  tone={i === 0 ? "locked" : "neutral"}
                  className="flex items-start justify-between gap-4 px-4 py-3.5"
                >
                  <div className="flex flex-col gap-0.5">
                    <div className="flex items-center gap-2">
                      <span className="text-[14px] font-semibold text-ink">{r.customer_name}</span>
                      {i === 0 && <Pill tone="locked">Best fit</Pill>}
                    </div>
                    <span className="text-[12.5px] text-ink-muted">
                      Currently {r.currently_scheduled ? formatDateShort(r.currently_scheduled) : "—"} ·
                      would move to {formatWindow(r.window)}
                    </span>
                    <span className="mt-0.5 text-[11.5px] text-ink-faint">
                      Agreed to an earlier delivery when they booked.
                    </span>
                  </div>
                  <div className="flex shrink-0 flex-col items-end">
                    <span className="font-display text-[24px] leading-none text-ink tnum">
                      {r.score}
                    </span>
                    <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-faint">
                      cost
                    </span>
                  </div>
                </Card>
              ))}
            </ol>
          )}
          <p className="text-[12.5px] text-ink-muted">
            These are candidates, not changes. Each would be offered the slot and could decline —
            the recovery loop is capped at two offers before it escalates to a coordinator.
          </p>
        </section>
      )}

      {/* -- the fleet ------------------------------------------------------ */}
      <div className="enter grid grid-cols-[minmax(0,1fr)_300px] gap-6" style={{ animationDelay: "60ms" }}>
        <section className="flex flex-col gap-3.5">
          <SectionHeader
            eyebrow={`${scheduled.length} committed`}
            title="Scheduled deliveries"
            description="Mark an order delayed to simulate the ERP signal. Its slot is freed, the day rebuilt, and replacements searched for."
          />
          {orders.initialising ? (
            <LoadingPanel rows={3} label="Loading deliveries" />
          ) : scheduled.length === 0 ? (
            <EmptyPanel
              title="Nothing scheduled"
              description="Confirm a booking first — there is no slot to lose until something is committed."
            />
          ) : (
            <ul className="flex flex-col gap-2">
              {scheduled.map((order) => {
                const isDelayed = order.readiness_status === "delayed";
                return (
                  <Card
                    key={order.id}
                    as="li"
                    settled={!isDelayed}
                    tone={isDelayed ? "alert" : "locked"}
                    className={cx("flex items-center justify-between gap-4 px-4 py-3", isDelayed && "stripe-pending")}
                  >
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-[13.5px] font-medium text-ink">
                          {order.customer_name}
                        </span>
                        {order.can_deliver_early && (
                          <Pill tone="accent">Open to earlier</Pill>
                        )}
                        {isDelayed && <Pill tone="alert">Goods delayed</Pill>}
                      </div>
                      <span className="font-mono text-[11.5px] text-ink-muted">
                        {order.delivery_date && formatDateShort(order.delivery_date)}
                        {order.locked_window && ` · ${formatWindow(order.locked_window)}`}
                        {" · "}
                        {order.job_type}
                      </span>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {order.locked_window && !isDelayed && (
                        <span className="flex items-center gap-1 text-[11.5px] text-locked">
                          <LockIcon />
                          locked
                        </span>
                      )}
                      <Button
                        variant={isDelayed ? "secondary" : "danger"}
                        busy={busyId === order.id}
                        onClick={() => setReadiness(order, isDelayed ? "ready" : "delayed")}
                      >
                        {isDelayed ? "Mark ready" : "Mark delayed"}
                      </Button>
                    </div>
                  </Card>
                );
              })}
            </ul>
          )}
        </section>

        <aside className="flex flex-col gap-3.5">
          <SectionHeader eyebrow="Consent" title="Who can be asked" />
          <Card className="flex flex-col gap-2.5 px-4 py-3.5">
            <p className="text-[12.5px] leading-[1.5] text-ink-muted">
              Only these customers agreed, at booking, that an earlier delivery would suit them.
              Nobody else is ever approached.
            </p>
            <ul className="flex flex-col gap-1.5 border-t border-rail pt-2.5">
              {flexible.length === 0 ? (
                <li className="text-[12.5px] text-ink-faint">
                  Nobody has opted in, so a freed slot would sit idle.
                </li>
              ) : (
                flexible.map((o) => (
                  <li key={o.id} className="flex items-center justify-between gap-2">
                    <span className="truncate text-[12.5px] text-ink-soft">{o.customer_name}</span>
                    <span className="shrink-0 font-mono text-[11px] text-ink-faint">
                      {o.delivery_date && formatDateShort(o.delivery_date)}
                    </span>
                  </li>
                ))
              )}
            </ul>
          </Card>

          {delayed.length > 0 && (
            <Card settled tone="alert" className="flex flex-col gap-1 px-4 py-3.5">
              <Eyebrow>Currently delayed</Eyebrow>
              {delayed.map((o) => (
                <span key={o.id} className="text-[12.5px] text-ink-soft">
                  {o.customer_name} · {o.delivery_date && formatDateShort(o.delivery_date)}
                </span>
              ))}
            </Card>
          )}
        </aside>
      </div>
    </Page>
  );
}
