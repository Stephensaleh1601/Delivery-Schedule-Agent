"use client";

import { useEffect, useRef, useState } from "react";
import { Page } from "@/components/Shell";
import { Button, Card, Eyebrow, ErrorPanel, LockIcon, Pill, Skeleton, cx } from "@/components/ui";
import { dispatch, type Evaluation, type Offer, type Horizon } from "@/lib/api";
import { formatDate, formatDuration, formatWindow } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * The customer's side of the promise.
 *
 * Every bubble here is the result of a real request. When the agent says two slots work, it is
 * because the solver said so; when the confirmation appears, the appointment is already locked in
 * the database and the day's plan has been republished. Nothing is staged in browser state.
 *
 * The panel on the right is not something a customer would ever see -- it is there so a viewer
 * can watch what the agent did while the customer was waiting.
 */

type Bubble =
  | { kind: "bot"; id: string; text: string }
  | { kind: "customer"; id: string; text: string }
  | { kind: "typing"; id: string }
  | { kind: "offer"; id: string; offer: Offer }
  | { kind: "confirmed"; id: string; text: string; date: string; window: string };

let bubbleSeq = 0;
const nextId = () => `b${++bubbleSeq}`;

export default function ConversationPage() {
  const horizon = useResource(() => dispatch.horizon(), []);
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [phase, setPhase] = useState<"intro" | "booking" | "waiting" | "offered" | "done">("intro");
  const [orderId, setOrderId] = useState<string | null>(null);
  const [evaluations, setEvaluations] = useState<Evaluation[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (horizon.data && bubbles.length === 0) {
      setBubbles([
        {
          kind: "bot",
          id: nextId(),
          text: "Hi! This is Majestic Fighters Furniture Delivery. I can book a delivery for your sofa, bed or cabinet.",
        },
        {
          kind: "bot",
          id: nextId(),
          text: `Tell me two or three times that would work for you between ${formatDate(
            horizon.data.first,
          )} and ${formatDate(horizon.data.last)}, and I'll check which of them we can actually make.`,
        },
      ]);
      setPhase("booking");
    }
  }, [horizon.data, bubbles.length]);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
  }, [bubbles]);

  const push = (b: Bubble) => setBubbles((prev) => [...prev, b]);
  const dropTyping = () => setBubbles((prev) => prev.filter((b) => b.kind !== "typing"));

  async function submitBooking(payload: BookingPayload, h: Horizon) {
    setBusy(true);
    setError(null);
    push({
      kind: "customer",
      id: nextId(),
      text: `${payload.job_type} delivery for ${payload.customer_name}, ${payload.address_raw}. I'm free:\n${payload.availability
        .map((a) => `• ${formatDate(a.date)}, ${formatWindow({ start: a.window_start, end: a.window_end })}`)
        .join("\n")}`,
    });
    setPhase("waiting");
    push({ kind: "bot", id: nextId(), text: "Thanks — let me check those against the vans we already have out." });
    push({ kind: "typing", id: nextId() });

    try {
      const created = await dispatch.createOrder(payload);
      setOrderId(created.id);
      // ONE planning call. Calling planAgentic and then planOptions opened two negotiations
      // with different slots in each -- the customer saw one pair, the log recorded the other.
      const options = await dispatch.planAgentic(created.id);
      setEvaluations(options.evaluations);
      dropTyping();

      if (!options.offer) {
        push({
          kind: "bot",
          id: nextId(),
          text:
            options.error ??
            "I'm sorry — none of those windows will work. One of our team will call you to sort out a time.",
        });
        setPhase("done");
        return;
      }
      push({ kind: "bot", id: nextId(), text: options.message ?? "We can deliver on:" });
      push({ kind: "offer", id: nextId(), offer: options.offer });
      setPhase("offered");
    } catch (err) {
      dropTyping();
      setError(err);
      setPhase("booking");
    } finally {
      setBusy(false);
    }
  }

  async function accept(offer: Offer, slotId: string) {
    const slot = offer.options.find((s) => s.id === slotId);
    if (!slot) return;
    setBusy(true);
    push({ kind: "customer", id: nextId(), text: `${slot.label} works for me.` });
    push({ kind: "typing", id: nextId() });
    try {
      const result = await dispatch.respond(offer.id, true, slotId);
      dropTyping();
      setBubbles((prev) => prev.filter((b) => b.kind !== "offer"));
      push({
        kind: "confirmed",
        id: nextId(),
        text: result.message ?? "You're confirmed.",
        date: result.delivery_date ?? slot.date,
        window: formatWindow(slot.window),
      });
      setPhase("done");
    } catch (err) {
      dropTyping();
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    bubbleSeq = 0;
    setBubbles([]);
    setOrderId(null);
    setEvaluations([]);
    setError(null);
    setPhase("intro");
    horizon.reload();
  }

  return (
    <Page
      title="Customer conversation"
      lede="A simulated WhatsApp thread wired to the live service. Each reply below is the result of a real request — the offer comes from the solver, and the confirmation locks the appointment before the message appears."
      actions={
        <Button variant="ghost" onClick={reset}>
          Start over
        </Button>
      }
    >
      {horizon.error ? <ErrorPanel error={horizon.error} onRetry={horizon.reload} /> : null}

      <div className="enter grid grid-cols-[minmax(0,420px)_minmax(0,1fr)] gap-6">
        {/* -- the thread ---------------------------------------------------- */}
        <Card className="flex h-[620px] flex-col overflow-hidden p-0">
          <div className="flex items-center gap-3 border-b border-rail px-4 py-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-full bg-accent-wash font-display text-[15px] text-accent">
              MF
            </div>
            <div className="flex flex-col">
              <span className="text-[13.5px] font-semibold text-ink">Majestic Fighters</span>
              <span className="text-[11.5px] text-ink-faint">Delivery scheduling</span>
            </div>
            <Pill tone={phase === "done" ? "locked" : "accent"}>
              {phase === "done" ? "Confirmed" : phase === "waiting" ? "Checking…" : "Online"}
            </Pill>
          </div>

          <div ref={feedRef} className="flex flex-1 flex-col gap-2.5 overflow-y-auto bg-canvas/60 px-4 py-4">
            {horizon.initialising ? (
              <>
                <Skeleton className="h-14 w-[75%]" />
                <Skeleton className="h-10 w-[60%]" />
              </>
            ) : (
              bubbles.map((b) => <BubbleView key={b.id} bubble={b} onAccept={accept} busy={busy} />)
            )}
          </div>

          <div className="border-t border-rail bg-surface px-4 py-3.5">
            {error ? (
              <ErrorPanel error={error} onRetry={() => setError(null)} />
            ) : phase === "booking" && horizon.data ? (
              <BookingForm horizon={horizon.data} busy={busy} onSubmit={(p) => submitBooking(p, horizon.data!)} />
            ) : phase === "waiting" ? (
              <p className="text-[12.5px] text-ink-muted">
                Solving each requested window against the day's route…
              </p>
            ) : phase === "offered" ? (
              <p className="text-[12.5px] text-ink-muted">Tap a time above to confirm it.</p>
            ) : (
              <p className="text-[12.5px] text-ink-muted">
                Booked. The window is locked and the day's plan has been republished.
              </p>
            )}
          </div>
        </Card>

        {/* -- what the customer never sees ---------------------------------- */}
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <Eyebrow>Behind the reply</Eyebrow>
            <h2 className="text-[17px] font-semibold text-ink">
              Why those two times, and not the one they asked for first
            </h2>
            <p className="max-w-[58ch] text-[13px] text-ink-muted">
              The customer is never told any of this. It is here so you can see the offer was
              earned by a solve, not chosen by preference order.
            </p>
          </div>

          {evaluations.length === 0 ? (
            <Card className="flex flex-1 flex-col items-center justify-center gap-2 border-dashed px-6 py-10 text-center">
              <span className="text-[13.5px] font-medium text-ink-soft">
                Nothing evaluated yet
              </span>
              <p className="max-w-[40ch] text-[12.5px] text-ink-muted">
                Submit the booking form and each requested window will be solved against the real
                route for that day.
              </p>
            </Card>
          ) : (
            <ol className="flex flex-col gap-2.5">
              {evaluations.map((e, i) => (
                <EvaluationRow key={e.availability_option_id} evaluation={e} rank={i} />
              ))}
            </ol>
          )}

          {orderId && (
            <p className="font-mono text-[11px] text-ink-faint">order {orderId}</p>
          )}
        </div>
      </div>
    </Page>
  );
}

// -- bubbles ------------------------------------------------------------------

function BubbleView({
  bubble,
  onAccept,
  busy,
}: {
  bubble: Bubble;
  onAccept: (offer: Offer, slotId: string) => void;
  busy: boolean;
}) {
  if (bubble.kind === "typing") {
    return (
      <div className="enter flex w-fit gap-1 rounded-[14px] rounded-bl-[4px] border border-rail bg-surface px-3.5 py-3">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-faint"
            style={{ animationDelay: `${i * 130}ms`, animationDuration: "1.1s" }}
          />
        ))}
        <span className="sr-only">The agent is checking availability</span>
      </div>
    );
  }

  if (bubble.kind === "customer") {
    return (
      <div className="enter max-w-[85%] self-end whitespace-pre-line rounded-[14px] rounded-br-[4px] bg-accent px-3.5 py-2.5 text-[13.5px] leading-[1.5] text-white">
        {bubble.text}
      </div>
    );
  }

  if (bubble.kind === "confirmed") {
    return (
      <div className="settle enter max-w-[88%] self-start rounded-[14px] rounded-bl-[4px] border border-locked-edge bg-locked-wash px-3.5 py-3">
        <div className="flex items-center gap-1.5 text-locked">
          <LockIcon />
          <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.11em]">
            Appointment locked
          </span>
        </div>
        <p className="mt-1.5 text-[13.5px] leading-[1.5] text-ink">{bubble.text}</p>
        <p className="mt-1 font-mono text-[11.5px] text-locked">
          {bubble.date} · {bubble.window}
        </p>
      </div>
    );
  }

  if (bubble.kind === "offer") {
    return (
      <div className="enter flex max-w-[88%] flex-col gap-1.5">
        {bubble.offer.options.map((slot) => (
          <button
            key={slot.id}
            disabled={busy}
            onClick={() => onAccept(bubble.offer, slot.id)}
            className={cx(
              "group flex items-center justify-between gap-3 rounded-[12px] border border-accent-edge",
              "bg-surface px-3.5 py-2.5 text-left transition-all duration-150",
              "hover:border-accent hover:bg-accent-wash disabled:opacity-50",
            )}
          >
            <span className="text-[13.5px] font-medium text-ink">{slot.label}</span>
            <span className="font-mono text-[11px] text-accent opacity-0 transition-opacity group-hover:opacity-100">
              choose →
            </span>
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="enter max-w-[85%] self-start whitespace-pre-line rounded-[14px] rounded-bl-[4px] border border-rail bg-surface px-3.5 py-2.5 text-[13.5px] leading-[1.5] text-ink">
      {bubble.text}
    </div>
  );
}

// -- evaluation ---------------------------------------------------------------

function EvaluationRow({ evaluation: e, rank }: { evaluation: Evaluation; rank: number }) {
  const recommended = rank === 0 && e.feasible;

  if (!e.feasible) {
    return (
      <Card as="li" settled tone="alert" className="px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <span className="text-[13.5px] font-medium text-ink-soft">
            {formatDate(e.date)}, {formatWindow(e.window)}
          </span>
          <Pill tone="alert">Can't be served</Pill>
        </div>
        <p className="mt-1 text-[12.5px] leading-[1.5] text-ink-muted">{e.infeasible_reason}</p>
      </Card>
    );
  }

  const b = e.breakdown;
  const parts = [
    { label: "extra driving", value: b.incremental_drive_minutes, tone: "neutral" as const },
    { label: "opens an empty day", value: b.day_opening_penalty_minutes, tone: "pending" as const },
    { label: "lower preference", value: b.preference_penalty_minutes, tone: "neutral" as const },
    { label: "runs late", value: b.overtime_penalty_minutes, tone: "pending" as const },
  ].filter((p) => p.value !== 0);

  return (
    <Card as="li" tone={recommended ? "locked" : "neutral"} className="px-4 py-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-semibold text-ink">{formatDate(e.date)}</span>
            {recommended && <Pill tone="locked">Recommended</Pill>}
          </div>
          <span className="font-mono text-[11.5px] text-ink-muted">{formatWindow(e.window)}</span>
        </div>
        <div className="flex flex-col items-end">
          <span
            className={cx(
              "font-display text-[26px] leading-none tnum",
              recommended ? "text-locked" : "text-ink-soft",
            )}
          >
            {e.total_score}
          </span>
          <span className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-ink-faint">
            cost
          </span>
        </div>
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-rail pt-2.5">
        {parts.length === 0 ? (
          <span className="text-[12px] text-ink-muted">Slots into the day at no extra cost.</span>
        ) : (
          parts.map((p) => (
            <span key={p.label} className="flex items-baseline gap-1 text-[12px]">
              <span
                className={cx(
                  "font-mono font-medium tnum",
                  p.tone === "pending" ? "text-pending" : "text-ink-soft",
                )}
              >
                +{p.value}m
              </span>
              <span className="text-ink-muted">{p.label}</span>
            </span>
          ))
        )}
      </div>

      <p className="mt-1.5 text-[11.5px] text-ink-faint">
        Day goes from {formatDuration(e.baseline_drive_minutes)} to{" "}
        {formatDuration(e.proposed_drive_minutes)} of driving.
      </p>
    </Card>
  );
}

// -- booking form -------------------------------------------------------------

interface BookingPayload {
  customer_name: string;
  phone: string;
  address_raw: string;
  postal_code: string;
  job_type: string;
  can_deliver_early: boolean;
  availability: Array<{ date: string; window_start: string; window_end: string; preference_rank: number }>;
}

const WINDOW_CHOICES = [
  { label: "Morning", start: "09:00", end: "13:00" },
  { label: "Afternoon", start: "13:00", end: "18:00" },
  { label: "Any time", start: "09:00", end: "18:00" },
];

function BookingForm({
  horizon,
  busy,
  onSubmit,
}: {
  horizon: Horizon;
  busy: boolean;
  onSubmit: (payload: BookingPayload) => void;
}) {
  const [name, setName] = useState("Mrs Lee");
  const [postal, setPostal] = useState("469123");
  const [jobType, setJobType] = useState("cabinet");
  const [early, setEarly] = useState(false);
  const [choices, setChoices] = useState([
    { date: horizon.dates[2], window: 2 },
    { date: horizon.dates[0], window: 0 },
    { date: horizon.dates[1], window: 1 },
  ]);

  function update(i: number, patch: Partial<{ date: string; window: number }>) {
    setChoices((prev) => prev.map((c, idx) => (idx === i ? { ...c, ...patch } : c)));
  }

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(ev) => {
        ev.preventDefault();
        onSubmit({
          customer_name: name,
          phone: "91112222",
          address_raw: `Blk ${postal.slice(0, 3)} Delivery Address`,
          postal_code: postal,
          job_type: jobType,
          can_deliver_early: early,
          availability: choices.map((c, i) => ({
            date: c.date,
            window_start: WINDOW_CHOICES[c.window].start,
            window_end: WINDOW_CHOICES[c.window].end,
            preference_rank: i + 1,
          })),
        });
      }}
    >
      <div className="grid grid-cols-[1fr_92px_110px] gap-2">
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            className={inputClass}
          />
        </Field>
        <Field label="Postal">
          <input
            value={postal}
            onChange={(e) => setPostal(e.target.value)}
            pattern="\d{6}"
            maxLength={6}
            inputMode="numeric"
            required
            className={cx(inputClass, "font-mono")}
          />
        </Field>
        <Field label="Item">
          <select value={jobType} onChange={(e) => setJobType(e.target.value)} className={inputClass}>
            <option value="sofa">Sofa</option>
            <option value="bed">Bed</option>
            <option value="cabinet">Cabinet</option>
            <option value="other">Other</option>
          </select>
        </Field>
      </div>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
          Times that would work — in order of preference
        </legend>
        {choices.map((c, i) => (
          <div key={i} className="grid grid-cols-[16px_1fr_1fr] items-center gap-2">
            <span className="font-mono text-[11px] text-ink-faint">{i + 1}</span>
            <select
              value={c.date}
              onChange={(e) => update(i, { date: e.target.value })}
              className={inputClass}
              aria-label={`Preference ${i + 1} date`}
            >
              {horizon.dates.map((d) => (
                <option key={d} value={d}>
                  {formatDate(d)}
                </option>
              ))}
            </select>
            <select
              value={c.window}
              onChange={(e) => update(i, { window: Number(e.target.value) })}
              className={inputClass}
              aria-label={`Preference ${i + 1} window`}
            >
              {WINDOW_CHOICES.map((w, idx) => (
                <option key={w.label} value={idx}>
                  {w.label}
                </option>
              ))}
            </select>
          </div>
        ))}
      </fieldset>

      <label className="flex items-center gap-2 text-[12.5px] text-ink-soft">
        <input
          type="checkbox"
          checked={early}
          onChange={(e) => setEarly(e.target.checked)}
          className="h-3.5 w-3.5 accent-[var(--color-accent)]"
        />
        I'd take an earlier slot if one frees up
      </label>

      <Button type="submit" variant="primary" busy={busy} className="w-full">
        Send to the delivery team
      </Button>
    </form>
  );
}

const inputClass =
  "w-full rounded-[9px] border border-rail-strong bg-surface px-2.5 py-1.5 text-[13px] text-ink " +
  "transition-colors focus:border-accent focus:outline-none";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
        {label}
      </span>
      {children}
    </label>
  );
}
