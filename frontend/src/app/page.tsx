"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { AgentDrawer } from "@/components/AgentDrawer";
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
  Skeleton,
  cx,
  statusTone,
} from "@/components/ui";
import { dispatch, type Order, type PlanningStatus } from "@/lib/api";
import { STATUS_LABELS, formatDateShort, formatDuration, formatWindow, orderLabel } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * Every booking, as a database.
 *
 * A spreadsheet on purpose. It is the view an operations person already knows how to read, and it
 * answers "what is the state of the business" in one screen -- which is the first of the two
 * questions someone seeing this product has. The status column carries the story: orders arrive
 * with no date, get offered slots, and become locked promises.
 *
 * Read-only. There is no write path behind it, and inventing one would mean a judge could break
 * the demo data mid-recording.
 */

type SortKey = "customer_name" | "planning_status" | "delivery_date" | "postal_code" | "job_type";

const COLUMNS: Array<{ key: SortKey | null; label: string; align?: "right"; width?: string }> = [
  { key: "customer_name", label: "Customer", width: "minmax(150px,1.3fr)" },
  { key: "postal_code", label: "Postal", width: "82px" },
  { key: null, label: "Address", width: "minmax(160px,1.6fr)" },
  { key: "job_type", label: "Order", width: "132px" },
  { key: null, label: "Mins", width: "56px", align: "right" },
  { key: null, label: "Requested windows", width: "minmax(150px,1.2fr)" },
  { key: "planning_status", label: "Status", width: "132px" },
  { key: "delivery_date", label: "Confirmed", width: "104px" },
  { key: null, label: "Locked window", width: "126px" },
  { key: null, label: "Flags", width: "120px" },
];

const GRID = COLUMNS.map((c) => c.width).join(" ");

export default function OrdersPage() {
  const orders = useResource(() => dispatch.orders(), []);
  const metrics = useResource(() => dispatch.metrics(), []);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<PlanningStatus | "all">("all");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({
    key: "planning_status",
    desc: false,
  });
  const [selected, setSelected] = useState<Order | null>(null);

  const all = orders.data ?? [];

  const facets = useMemo(() => {
    const counts = new Map<string, number>();
    for (const o of all) counts.set(o.planning_status, (counts.get(o.planning_status) ?? 0) + 1);
    return counts;
  }, [all]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = all.filter((o) => {
      if (status !== "all" && o.planning_status !== status) return false;
      if (!needle) return true;
      return (
        o.customer_name.toLowerCase().includes(needle) ||
        (o.postal_code ?? "").includes(needle) ||
        o.job_type.includes(needle) ||
        (o.address ?? "").toLowerCase().includes(needle)
      );
    });
    return [...filtered].sort((a, b) => {
      const av = String(a[sort.key] ?? "");
      const bv = String(b[sort.key] ?? "");
      return sort.desc ? bv.localeCompare(av) : av.localeCompare(bv);
    });
  }, [all, query, status, sort]);

  if (orders.error) {
    return (
      <Page title="Orders" lede="Every booking and where it stands.">
        <ErrorPanel error={orders.error} onRetry={orders.reload} />
      </Page>
    );
  }

  return (
    <Page
      title="Orders"
      lede="Every delivery this company has agreed to, or is still working out. An order arrives with no date — the agent works out which day it should land on, and once a customer accepts, that window is locked."
      wide
      actions={
        <Link href="/chat">
          <Button variant="primary">Book a delivery</Button>
        </Link>
      }
    >
      {/* -- what the agent is protecting ---------------------------------- */}
      <section className="enter grid grid-cols-[repeat(4,minmax(0,1fr))] gap-3">
        {metrics.initialising ? (
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[74px]" />)
        ) : metrics.data ? (
          <>
            <Headline
              value={metrics.data.confirmed_appointments_moved}
              label="Customers moved"
              note="Confirmed windows changed without asking"
              tone={metrics.data.confirmed_appointments_moved === 0 ? "locked" : "alert"}
            />
            <Headline
              value={all.filter((o) => o.locked_window).length}
              label="Locked promises"
              note="Protected through every replan"
            />
            <Headline
              value={metrics.data.scheduled_stops}
              label="Stops planned"
              note={`${metrics.data.horizon.first} – ${metrics.data.horizon.last}`}
            />
            <Headline
              value={formatDuration(metrics.data.round_trip_drive_minutes)}
              label="Driving"
              note="Across the whole horizon, round trip"
            />
          </>
        ) : null}
      </section>

      {/* -- filters ------------------------------------------------------- */}
      <section className="enter flex flex-wrap items-center gap-2" style={{ animationDelay: "40ms" }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by name, postal code, address…"
          aria-label="Filter orders"
          className="w-[280px] rounded-[9px] border border-rail-strong bg-surface px-3 py-1.5 text-[13px] text-ink focus:border-accent focus:outline-none"
        />
        <Facet active={status === "all"} onClick={() => setStatus("all")} count={all.length}>
          All
        </Facet>
        {(["pending_planning", "offered", "confirmed", "sequenced"] as PlanningStatus[])
          .filter((s) => facets.get(s))
          .map((s) => (
            <Facet
              key={s}
              active={status === s}
              onClick={() => setStatus(s)}
              count={facets.get(s) ?? 0}
              tone={statusTone(s)}
            >
              {STATUS_LABELS[s]}
            </Facet>
          ))}
        <span className="ml-auto font-mono text-[11.5px] text-ink-faint tnum">
          {rows.length} of {all.length}
        </span>
      </section>

      {/* -- the grid ------------------------------------------------------ */}
      {orders.initialising ? (
        <LoadingPanel rows={4} label="Loading orders" />
      ) : rows.length === 0 ? (
        <EmptyPanel
          title={all.length === 0 ? "No orders yet" : "Nothing matches that filter"}
          description={
            all.length === 0
              ? "Seed the demo database, or book one from Customer Chat."
              : "Try a different name, postal code or status."
          }
        />
      ) : (
        <section
          className="enter overflow-hidden rounded-[12px] border border-rail bg-surface"
          style={{ animationDelay: "80ms" }}
        >
          <div className="overflow-x-auto">
            <div role="table" aria-label="Orders" className="min-w-[1180px]">
              <div
                role="row"
                className="sticky top-0 z-10 grid border-b border-rail-strong bg-sunk"
                style={{ gridTemplateColumns: GRID }}
              >
                {COLUMNS.map((col) => (
                  <SortableHeader
                    key={col.label}
                    label={col.label}
                    align={col.align}
                    sortKey={col.key}
                    sort={sort}
                    onSort={setSort}
                  />
                ))}
              </div>

              {rows.map((order, i) => (
                <OrderRow
                  key={order.id}
                  order={order}
                  striped={i % 2 === 1}
                  onOpen={() => setSelected(order)}
                />
              ))}
            </div>
          </div>
        </section>
      )}

      <AgentDrawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        orderId={selected?.id}
      />
    </Page>
  );
}

// -- rows ---------------------------------------------------------------------

function OrderRow({
  order,
  striped,
  onOpen,
}: {
  order: Order;
  striped: boolean;
  onOpen: () => void;
}) {
  return (
    <div
      role="row"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen())}
      className={cx(
        "grid cursor-pointer border-b border-rail/70 text-[12.5px] transition-colors",
        striped ? "bg-canvas/40" : "bg-surface",
        "hover:bg-accent-wash/50",
      )}
      style={{ gridTemplateColumns: GRID }}
    >
      <Cell className="font-medium text-ink">{order.customer_name}</Cell>
      <Cell className="font-mono text-ink-muted">{order.postal_code ?? "—"}</Cell>
      <Cell className="truncate text-ink-muted" title={order.address}>
        {order.address}
      </Cell>
      <Cell className="text-ink-soft">{orderLabel(order.job_type)}</Cell>
      <Cell align="right" className="font-mono text-ink-muted tnum">
        {order.duration_minutes}
      </Cell>
      <Cell className="text-ink-muted">
        {order.availability_options.length === 0 ? (
          "—"
        ) : (
          <span className="flex flex-wrap gap-1">
            {order.availability_options.map((o) => (
              <span
                key={o.id}
                className="rounded-[4px] bg-sunk px-1.5 py-[1px] font-mono text-[10.5px] text-ink-muted"
                title={`${o.date} ${o.start}–${o.end}`}
              >
                {formatDateShort(o.date)}
              </span>
            ))}
          </span>
        )}
      </Cell>
      <Cell>
        <Pill tone={statusTone(order.planning_status)}>{STATUS_LABELS[order.planning_status]}</Pill>
      </Cell>
      <Cell className="font-mono text-ink-soft">
        {order.delivery_date ? formatDateShort(order.delivery_date) : "—"}
      </Cell>
      <Cell className="font-mono text-ink-soft">
        {order.locked_window ? (
          <span className="flex items-center gap-1 text-locked">
            <LockIcon />
            {formatWindow(order.locked_window)}
          </span>
        ) : (
          <span className="text-ink-faint">—</span>
        )}
      </Cell>
      <Cell>
        <span className="flex flex-wrap gap-1">
          {order.readiness_status === "delayed" && <Pill tone="alert">Delayed</Pill>}
          {order.can_deliver_early && <Pill tone="accent">Flexible</Pill>}
        </span>
      </Cell>
    </div>
  );
}

function Cell({
  children,
  className,
  align,
  title,
}: {
  children: React.ReactNode;
  className?: string;
  align?: "right";
  title?: string;
}) {
  return (
    <div
      role="cell"
      title={title}
      className={cx(
        "flex min-w-0 items-center border-r border-rail/40 px-2.5 py-2 last:border-r-0",
        align === "right" && "justify-end",
        className,
      )}
    >
      {children}
    </div>
  );
}

function SortableHeader({
  label,
  sortKey,
  sort,
  onSort,
  align,
}: {
  label: string;
  sortKey: SortKey | null;
  sort: { key: SortKey; desc: boolean };
  onSort: (s: { key: SortKey; desc: boolean }) => void;
  align?: "right";
}) {
  const active = sortKey !== null && sort.key === sortKey;
  return (
    <div
      role="columnheader"
      className={cx(
        "flex items-center gap-1 border-r border-rail px-2.5 py-2 last:border-r-0",
        align === "right" && "justify-end",
      )}
    >
      <button
        disabled={sortKey === null}
        onClick={() => sortKey && onSort({ key: sortKey, desc: active ? !sort.desc : false })}
        className={cx(
          "font-mono text-[10px] uppercase tracking-[0.1em] transition-colors",
          sortKey === null ? "cursor-default text-ink-faint" : "text-ink-muted hover:text-ink",
          active && "text-accent",
        )}
      >
        {label}
        {active && <span className="ml-1">{sort.desc ? "↓" : "↑"}</span>}
      </button>
    </div>
  );
}

function Facet({
  children,
  active,
  count,
  onClick,
  tone = "neutral",
}: {
  children: React.ReactNode;
  active: boolean;
  count: number;
  onClick: () => void;
  tone?: "neutral" | "locked" | "pending" | "alert" | "accent";
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-[8px] border px-2.5 py-1.5 text-[12.5px] transition-colors",
        active
          ? "border-accent bg-accent-wash text-accent"
          : "border-rail bg-surface text-ink-soft hover:bg-sunk",
      )}
    >
      {children}
      <span className={cx("font-mono text-[11px] tnum", active ? "text-accent" : "text-ink-faint")}>
        {count}
      </span>
    </button>
  );
}

function Headline({
  value,
  label,
  note,
  tone = "neutral",
}: {
  value: React.ReactNode;
  label: string;
  note: string;
  tone?: "neutral" | "locked" | "alert";
}) {
  return (
    <Card
      className={cx(
        "flex flex-col gap-0.5 px-4 py-3",
        tone === "locked" && "border-locked-edge bg-locked-wash",
        tone === "alert" && "border-alert-edge bg-alert-wash",
      )}
    >
      <Eyebrow>{label}</Eyebrow>
      <span
        className={cx(
          "font-display text-[27px] leading-[1.1] tnum",
          tone === "locked" ? "text-locked" : tone === "alert" ? "text-alert" : "text-ink",
        )}
      >
        {value}
      </span>
      <span className="text-[11.5px] leading-[1.4] text-ink-muted">{note}</span>
    </Card>
  );
}
