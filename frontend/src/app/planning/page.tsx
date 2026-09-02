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
  Pill,
  SectionHeader,
  Table,
  Td,
  Th,
  cx,
} from "@/components/ui";
import { dispatch, type Evaluation, type Order, type PlanOptions } from "@/lib/api";
import { formatDate, formatDateShort, formatDuration, formatWindow } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * Slot evaluation, shown as a coordinator would want to argue with it.
 *
 * The single score is never presented on its own. Its four components are always visible, because
 * a number nobody can decompose is a number nobody trusts -- and the one interesting case in this
 * product is precisely when the cheapest slot is NOT the customer's first choice.
 */
export default function PlanningPage() {
  const orders = useResource(() => dispatch.orders(), []);
  const [selected, setSelected] = useState<Order | null>(null);
  const [result, setResult] = useState<PlanOptions | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const pending = (orders.data ?? []).filter((o) =>
    ["pending_planning", "pending_availability"].includes(o.planning_status),
  );

  async function evaluate(order: Order) {
    setSelected(order);
    setResult(null);
    setError(null);
    setBusy(true);
    try {
      setResult(await dispatch.planOptions(order.id));
      orders.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (orders.error) {
    return (
      <Page title="Slot evaluation" lede="Price a customer's requested windows against the real route.">
        <ErrorPanel error={orders.error} onRetry={orders.reload} />
      </Page>
    );
  }

  return (
    <Page
      title="Slot evaluation"
      lede="Each window a customer offered is solved against that day's actual route, with and without them. The difference is what saying yes would cost."
    >
      <div className="enter grid grid-cols-[300px_minmax(0,1fr)] gap-6">
        {/* -- queue -------------------------------------------------------- */}
        <div className="flex flex-col gap-3">
          <SectionHeader eyebrow={`${pending.length} waiting`} title="Awaiting planning" />
          {orders.initialising ? (
            <LoadingPanel rows={3} label="Loading orders" />
          ) : pending.length === 0 ? (
            <EmptyPanel
              title="Nothing waiting"
              description="Every order has either been offered a slot or confirmed one."
            />
          ) : (
            <ul className="flex flex-col gap-2">
              {pending.map((order) => {
                const active = selected?.id === order.id;
                return (
                  <li key={order.id}>
                    <button
                      onClick={() => evaluate(order)}
                      className={cx(
                        "w-full rounded-[14px] border px-4 py-3 text-left transition-all duration-150",
                        active
                          ? "border-accent bg-accent-wash"
                          : "border-rail bg-surface shadow-[var(--shadow-raise)] hover:border-rail-strong",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-[13.5px] font-medium text-ink">
                          {order.customer_name}
                        </span>
                        <span className="shrink-0 font-mono text-[10.5px] text-ink-faint">
                          {order.duration_minutes}m
                        </span>
                      </div>
                      <p className="mt-0.5 text-[12px] text-ink-muted">
                        {order.job_type} · {order.postal_code}
                      </p>
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {order.availability_options.map((o) => (
                          <span
                            key={o.id}
                            className="rounded-[5px] bg-sunk px-1.5 py-[1px] font-mono text-[10px] text-ink-muted"
                          >
                            {formatDateShort(o.date)}
                          </span>
                        ))}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* -- evaluation --------------------------------------------------- */}
        <div className="flex flex-col gap-4">
          {!selected ? (
            <EmptyPanel
              title="Pick an order to evaluate"
              description="Every window that customer offered will be solved against the real route for that day, and the results ranked by what they cost the operation."
            />
          ) : (
            <>
              <SectionHeader
                eyebrow={`${selected.job_type} · ${selected.duration_minutes} minutes · ${selected.postal_code}`}
                title={selected.customer_name}
                description={
                  busy
                    ? "Solving each requested window…"
                    : "Ranked by total cost. Feasibility is a separate test — an impossible slot is never merely expensive."
                }
                actions={
                  <Button variant="secondary" busy={busy} onClick={() => evaluate(selected)}>
                    Re-evaluate
                  </Button>
                }
              />

              {error ? <ErrorPanel error={error} onRetry={() => evaluate(selected)} /> : null}
              {busy && !result && <LoadingPanel rows={3} label="Evaluating windows" />}

              {result?.error && !result.offer && (
                <Card settled tone="alert" className="px-5 py-4">
                  <Eyebrow>Escalated</Eyebrow>
                  <p className="mt-1 text-[13.5px] text-ink-soft">{result.error}</p>
                  <p className="mt-1 text-[12.5px] text-ink-muted">
                    A coordinator exception has been raised. The system will not invent a time the
                    customer did not offer.
                  </p>
                </Card>
              )}

              {result && result.evaluations.length > 0 && (
                <>
                  {result.offer && <RecommendationBanner result={result} />}
                  <EvaluationTable evaluations={result.evaluations} offeredIds={
                    new Set(result.offer?.options.map((o) => o.availability_option_id) ?? [])
                  } />
                </>
              )}
            </>
          )}
        </div>
      </div>
    </Page>
  );
}

function RecommendationBanner({ result }: { result: PlanOptions }) {
  const best = result.evaluations.find((e) => e.feasible);
  if (!best || !result.offer) return null;

  const preferredFirst = result.evaluations[0]?.breakdown.preference_penalty_minutes === 0;

  return (
    <Card tone="locked" className="settle flex items-start justify-between gap-6 px-5 py-4">
      <div className="flex flex-col gap-1">
        <Eyebrow>Recommended</Eyebrow>
        <span className="font-display text-[24px] leading-[1.15] text-ink">
          {formatDate(best.date)}, {formatWindow(best.window)}
        </span>
        <p className="max-w-[54ch] text-[13px] leading-[1.5] text-ink-muted">
          {best.breakdown.day_opening_penalty_minutes === 0
            ? "Slots into a day the van is already working."
            : "Every cheaper option was infeasible, so this opens a new delivery day."}
          {!preferredFirst &&
            " This is not the customer's first choice — that one costs more to serve, and was offered second."}
        </p>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <span className="font-display text-[38px] leading-none text-locked tnum">
          {best.total_score}
        </span>
        <span className="font-mono text-[9.5px] uppercase tracking-[0.11em] text-ink-faint">
          minutes of cost
        </span>
        <Pill tone="accent">{result.offer.options.length} offered</Pill>
      </div>
    </Card>
  );
}

function EvaluationTable({
  evaluations,
  offeredIds,
}: {
  evaluations: Evaluation[];
  offeredIds: Set<string>;
}) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Requested window</Th>
          <Th align="right">Extra driving</Th>
          <Th align="right">Empty day</Th>
          <Th align="right">Preference</Th>
          <Th align="right">Overtime</Th>
          <Th align="right">Total</Th>
          <Th>Outcome</Th>
        </tr>
      </thead>
      <tbody>
        {evaluations.map((e) => {
          const offered = offeredIds.has(e.availability_option_id);
          const b = e.breakdown;
          if (!e.feasible) {
            return (
              <tr key={e.availability_option_id} className="bg-alert-wash/40">
                <Td>
                  <span className="text-[13px] font-medium text-ink-soft">{formatDate(e.date)}</span>
                  <span className="ml-2 font-mono text-[11.5px] text-ink-muted">
                    {formatWindow(e.window)}
                  </span>
                </Td>
                <Td colSpan={4} className="text-[12.5px] text-ink-muted">
                  {e.infeasible_reason}
                </Td>
                <Td align="right" className="font-mono text-ink-faint">
                  —
                </Td>
                <Td>
                  <Pill tone="alert">Not possible</Pill>
                </Td>
              </tr>
            );
          }
          return (
            <tr key={e.availability_option_id} className={offered ? "bg-locked-wash/35" : undefined}>
              <Td>
                <span className="text-[13px] font-medium text-ink">{formatDate(e.date)}</span>
                <span className="ml-2 font-mono text-[11.5px] text-ink-muted">
                  {formatWindow(e.window)}
                </span>
                <div className="mt-0.5 text-[11.5px] text-ink-faint">
                  {formatDuration(e.baseline_drive_minutes)} → {formatDuration(e.proposed_drive_minutes)}
                </div>
              </Td>
              <Cost value={b.incremental_drive_minutes} />
              <Cost value={b.day_opening_penalty_minutes} tone="pending" />
              <Cost value={b.preference_penalty_minutes} />
              <Cost value={b.overtime_penalty_minutes} tone="pending" />
              <Td align="right">
                <span className="font-display text-[21px] leading-none text-ink tnum">
                  {e.total_score}
                </span>
              </Td>
              <Td>{offered ? <Pill tone="locked">Offered</Pill> : <Pill tone="neutral">Held back</Pill>}</Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

function Cost({ value, tone = "neutral" }: { value: number; tone?: "neutral" | "pending" }) {
  return (
    <Td align="right">
      <span
        className={cx(
          "font-mono text-[13px] tnum",
          value === 0 ? "text-ink-faint" : tone === "pending" ? "text-pending" : "text-ink-soft",
        )}
      >
        {value === 0 ? "—" : `+${value}`}
      </span>
    </Td>
  );
}
