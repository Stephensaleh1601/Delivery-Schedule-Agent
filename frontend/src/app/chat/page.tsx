"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ActionRow } from "@/components/AgentTrace";
import { DayChange, RouteImpactTable } from "@/components/RouteImpact";
import { RouteMap, stopsToPoints } from "@/components/RouteMap";
import { Page } from "@/components/Shell";
import {
  Bubble,
  ChoiceBubble,
  Composer,
  Phone,
  TypingBubble,
  Wallpaper,
  chatTime,
} from "@/components/WhatsApp";
import { Button, Card, ErrorPanel, Eyebrow, LockIcon, Pill, Skeleton, cx } from "@/components/ui";
import {
  dispatch,
  type ActivePlan,
  type AgentRun,
  type Bootstrap,
  type Evaluation,
  type Offer,
  type PlanVersion,
} from "@/lib/api";
import { formatDate, formatDuration, formatWindow } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * The customer's side, and the agent's working, on one screen.
 *
 * Side by side because the claim only lands if you see both at once: the customer is told two
 * friendly times, and beside it is the evidence that those two were chosen by solving every window
 * they offered against the real route. Neither half is convincing alone.
 *
 * Everything is a real request. When the confirmation appears the appointment is already locked and
 * the day's plan republished -- the UI is reporting, not performing.
 */

type Msg =
  | { kind: "them"; id: string; text: string; time: string }
  | { kind: "me"; id: string; text: string; time: string }
  | { kind: "typing"; id: string }
  | { kind: "choices"; id: string; offer: Offer }
  | { kind: "confirmed"; id: string; text: string; time: string };

let seq = 0;
const nextId = () => `b${++seq}`;

const WINDOWS = [
  { label: "Morning", start: "09:00", end: "13:00" },
  { label: "Afternoon", start: "13:00", end: "18:00" },
  { label: "Any time", start: "09:00", end: "18:00" },
];

export default function ChatPage() {
  const router = useRouter();
  const boot = useResource(() => dispatch.bootstrap(), []);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "waiting" | "offered" | "done">("loading");
  const [orderId, setOrderId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [evaluations, setEvaluations] = useState<Evaluation[]>([]);
  const [before, setBefore] = useState<PlanVersion | null>(null);
  const [after, setAfter] = useState<ActivePlan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const feed = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!boot.data || msgs.length) return;
    const h = boot.data.horizon;
    setMsgs([
      {
        kind: "them", id: nextId(), time: chatTime(-4),
        text: "Hi! This is Majestic Fighters Furniture Delivery. I can book your sofa, bed or cabinet delivery.",
      },
      {
        kind: "them", id: nextId(), time: chatTime(-4),
        text: `Give me two or three times that would work between ${formatDate(h.first)} and ${formatDate(h.last)}, and I'll check which of them we can actually make.`,
      },
    ]);
    setPhase("ready");
  }, [boot.data, msgs.length]);

  useEffect(() => {
    feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: "smooth" });
  }, [msgs]);

  const push = (m: Msg) => setMsgs((prev) => [...prev, m]);
  const dropTyping = () => setMsgs((prev) => prev.filter((m) => m.kind !== "typing"));

  async function book(payload: BookingPayload) {
    setBusy(true);
    setError(null);
    push({
      kind: "me", id: nextId(), time: chatTime(),
      text: `${payload.job_type} delivery for ${payload.customer_name}.\nI'm free:\n${payload.availability
        .map((a) => `• ${formatDate(a.date)}, ${a.window_start}–${a.window_end}`)
        .join("\n")}`,
    });
    setPhase("waiting");
    push({ kind: "typing", id: nextId() });

    try {
      const created = await dispatch.createOrder(payload);
      setOrderId(created.id);
      // ONE planning call. It returns the offer AND the evaluations behind it, so what the
      // customer reads and what this panel shows cannot disagree.
      const result = await dispatch.planAgentic(created.id);
      setRun(result.run ?? null);
      setEvaluations(result.evaluations);
      dropTyping();

      if (!result.offer) {
        push({
          kind: "them", id: nextId(), time: chatTime(),
          text: "I'm sorry — none of those will work. One of our team will call you to sort out a time.",
        });
        setPhase("done");
        return;
      }
      push({ kind: "them", id: nextId(), time: chatTime(), text: result.message ?? "We can deliver on:" });
      push({ kind: "choices", id: nextId(), offer: result.offer });
      setPhase("offered");
    } catch (err) {
      dropTyping();
      setError(err);
      setPhase("ready");
    } finally {
      setBusy(false);
    }
  }

  async function accept(offer: Offer, slotId: string) {
    const slot = offer.options.find((s) => s.id === slotId);
    if (!slot) return;
    setBusy(true);

    // The day as it stands BEFORE, so the change can be shown rather than asserted.
    const prior = await dispatch.planVersions(slot.date).catch(() => []);
    setBefore(prior.find((v) => v.status === "active") ?? null);

    setMsgs((prev) => prev.filter((m) => m.kind !== "choices"));
    push({ kind: "me", id: nextId(), time: chatTime(), text: slot.label });
    push({ kind: "typing", id: nextId() });
    try {
      const result = await dispatch.respond(offer.id, true, slotId);
      dropTyping();
      push({
        kind: "confirmed", id: nextId(), time: chatTime(),
        text: result.message ?? "You're confirmed.",
      });
      setAfter(await dispatch.activePlan(slot.date).catch(() => null));
      setPhase("done");
    } catch (err) {
      dropTyping();
      setError(err);
      setPhase("offered");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    seq = 0;
    setMsgs([]);
    setOrderId(null);
    setRun(null);
    setEvaluations([]);
    setBefore(null);
    setAfter(null);
    setError(null);
    setPhase("loading");
    boot.reload();
  }

  const status = phase === "waiting" ? "typing…" : phase === "done" ? "delivery confirmed" : "online";

  return (
    <Page
      title="Customer Chat"
      lede="The customer's side, and the agent's working, together. Every reply is the result of a real request — the offer comes from solving each window against the actual route, and the appointment is locked before the confirmation appears."
      wide
      actions={
        <Button variant="ghost" onClick={reset}>
          Start over
        </Button>
      }
    >
      {boot.error ? <ErrorPanel error={boot.error} onRetry={boot.reload} /> : null}

      <div className="enter grid grid-cols-[380px_minmax(0,1fr)] items-start gap-6">
        {/* -- the phone -------------------------------------------------- */}
        <div className="sticky top-[74px] flex flex-col gap-3">
          <Phone status={status}>
            <Wallpaper innerRef={feed}>
              <div className="flex min-h-[380px] flex-col gap-1.5">
                {boot.initialising ? (
                  <>
                    <Skeleton className="h-14 w-[75%]" />
                    <Skeleton className="h-10 w-[60%]" />
                  </>
                ) : (
                  msgs.map((m) => (
                    <MessageView key={m.id} msg={m} onChoose={accept} busy={busy} />
                  ))
                )}
              </div>
            </Wallpaper>
            <Composer>
              <span className="text-[12.5px] text-[color:var(--color-wa-meta)]">
                {phase === "ready"
                  ? "Fill in your details →"
                  : phase === "offered"
                    ? "Tap a time above"
                    : phase === "waiting"
                      ? "Checking availability…"
                      : "Message"}
              </span>
            </Composer>
          </Phone>

          {phase === "ready" && boot.data && (
            <Card className="px-4 py-3.5">
              <BookingForm boot={boot.data} busy={busy} onSubmit={book} />
            </Card>
          )}
          {error ? <ErrorPanel error={error} onRetry={() => setError(null)} /> : null}
        </div>

        {/* -- the agent -------------------------------------------------- */}
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <Eyebrow>Agent decision</Eyebrow>
            <h2 className="text-[17px] font-semibold text-ink">
              {phase === "ready" || phase === "loading"
                ? "Nothing decided yet"
                : phase === "waiting"
                  ? "Solving each requested window against the real route…"
                  : `${evaluations.filter((e) => e.feasible).length} of ${evaluations.length} requested windows can be served`}
            </h2>
            <p className="max-w-[68ch] text-[12.5px] text-ink-muted">
              The customer sees none of this. It is here so you can see the offer was earned by a
              solve rather than taken from their preference order.
            </p>
          </div>

          {run && (
            <Card className="overflow-hidden p-0">
              <div className="flex items-center justify-between gap-3 border-b border-rail px-4 py-2.5">
                <Eyebrow>Tools called</Eyebrow>
                <span className="font-mono text-[11px] text-ink-faint tnum">
                  {run.actions.length}/6 steps
                </span>
              </div>
              <ol className="flex flex-col">
                {run.actions.map((a) => (
                  <ActionRow key={a.step} action={a} />
                ))}
              </ol>
            </Card>
          )}

          {evaluations.length > 0 && (
            <section className="flex flex-col gap-2.5">
              <Eyebrow>Route impact per requested date</Eyebrow>
              <div className="grid grid-cols-2 gap-3">
                {evaluations.map((e, i) => (
                  <Card
                    key={e.availability_option_id}
                    tone={i === 0 && e.feasible ? "locked" : "neutral"}
                    className="flex flex-col gap-2.5 px-4 py-3.5"
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-[13.5px] font-semibold text-ink">
                        {formatDate(e.date)}
                      </span>
                      {i === 0 && e.feasible ? (
                        <Pill tone="locked">Recommended</Pill>
                      ) : (
                        <span className="font-mono text-[11px] text-ink-faint">
                          {formatWindow(e.promise_window ?? e.window)}
                        </span>
                      )}
                    </div>
                    <RouteImpactTable evaluation={e} />
                  </Card>
                ))}
              </div>
            </section>
          )}

          {/* -- what the confirmation did -------------------------------- */}
          {after && (
            <section className="settle flex flex-col gap-3 rounded-[14px] border border-locked-edge bg-locked-wash/45 p-4">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <Pill tone="locked" icon={<LockIcon />}>
                    Confirmed &amp; locked
                  </Pill>
                  <span className="text-[13.5px] text-ink-soft">
                    {formatDate(after.delivery_date)} — plan v{after.version}
                  </span>
                </div>
                <Button
                  variant="secondary"
                  onClick={() => {
                    sessionStorage.setItem(
                      "dispatch:highlight",
                      JSON.stringify({ jobId: orderId, date: after.delivery_date }),
                    );
                    router.push("/routes");
                  }}
                >
                  See the full day →
                </Button>
              </div>

              <div className="grid grid-cols-[minmax(0,320px)_minmax(0,1fr)] gap-4">
                <div className="flex flex-col gap-2">
                  <DayChange
                    label="This day"
                    before={
                      before
                        ? {
                            drive_minutes: before.round_trip_drive_minutes,
                            stops: before.stop_count,
                            distance_km: before.round_trip_distance_km,
                            finishes_at: before.finishes_at,
                          }
                        : null
                    }
                    after={{
                      drive_minutes: after.round_trip_drive_minutes,
                      stops: after.stop_count,
                      distance_km: after.round_trip_distance_km,
                      finishes_at: after.finishes_at,
                    }}
                  />
                  <p className="text-[11.5px] leading-[1.45] text-ink-muted">
                    Every other customer on this day kept the window they were promised.
                  </p>
                </div>
                <RouteMap
                  points={stopsToPoints(after.stops, orderId)}
                  depot={after.depot}
                  apiKey={boot.data?.map.google_maps_api_key}
                  className="h-[220px]"
                />
              </div>
            </section>
          )}

          {orderId && <p className="font-mono text-[11px] text-ink-faint">order {orderId}</p>}
        </div>
      </div>
    </Page>
  );
}

// -- thread -------------------------------------------------------------------

function MessageView({
  msg,
  onChoose,
  busy,
}: {
  msg: Msg;
  onChoose: (offer: Offer, slotId: string) => void;
  busy: boolean;
}) {
  if (msg.kind === "typing") return <TypingBubble />;

  if (msg.kind === "choices") {
    return (
      <ChoiceBubble
        disabled={busy}
        options={msg.offer.options.map((slot) => ({
          id: slot.id,
          label: slot.label,
          sub: "Tap to confirm this time",
        }))}
        onChoose={(id) => onChoose(msg.offer, id)}
      />
    );
  }

  if (msg.kind === "confirmed") {
    return (
      <Bubble from="them" time={msg.time} tone="confirmed">
        {msg.text}
      </Bubble>
    );
  }

  return (
    <Bubble
      from={msg.kind === "me" ? "me" : "them"}
      time={msg.time}
      ticks={msg.kind === "me" ? "read" : undefined}
    >
      {msg.text}
    </Bubble>
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

function BookingForm({
  boot,
  busy,
  onSubmit,
}: {
  boot: Bootstrap;
  busy: boolean;
  onSubmit: (payload: BookingPayload) => void;
}) {
  const dates = boot.horizon.dates;
  const [name, setName] = useState("Mrs Lee");
  const [postal, setPostal] = useState("460216");
  // A sofa (45 min) rather than a cabinet (105): with real drive times a cabinet no longer fits
  // the tighter windows, and the demo needs more than one feasible option to compare.
  const [jobType, setJobType] = useState("sofa");
  const [early, setEarly] = useState(false);
  // Defaults chosen so the demo's point is visible: the customer's FIRST preference is the empty
  // day, which the agent will rank second because opening it costs an hour.
  const [choices, setChoices] = useState([
    { date: dates[2], window: 2 },
    { date: dates[0], window: 0 },
    { date: dates[1], window: 1 },
  ]);

  return (
    <form
      className="flex flex-col gap-2.5"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({
          customer_name: name,
          phone: "91112222",
          address_raw: `Blk ${postal.slice(0, 3)}`,
          postal_code: postal,
          job_type: jobType,
          can_deliver_early: early,
          availability: choices.map((c, i) => ({
            date: c.date,
            window_start: WINDOWS[c.window].start,
            window_end: WINDOWS[c.window].end,
            preference_rank: i + 1,
          })),
        });
      }}
    >
      <Eyebrow>What the customer sends</Eyebrow>
      <div className="grid grid-cols-[1fr_88px_104px] gap-2">
        <Field label="Name">
          <input value={name} onChange={(e) => setName(e.target.value)} required className={input} />
        </Field>
        <Field label="Postal">
          <input
            value={postal}
            onChange={(e) => setPostal(e.target.value)}
            pattern="\d{6}"
            maxLength={6}
            inputMode="numeric"
            required
            className={cx(input, "font-mono")}
          />
        </Field>
        <Field label="Item">
          <select value={jobType} onChange={(e) => setJobType(e.target.value)} className={input}>
            <option value="sofa">Sofa</option>
            <option value="bed">Bed</option>
            <option value="cabinet">Cabinet</option>
            <option value="other">Other</option>
          </select>
        </Field>
      </div>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
          Times that work, best first
        </legend>
        {choices.map((c, i) => (
          <div key={i} className="grid grid-cols-[14px_1fr_1fr] items-center gap-2">
            <span className="font-mono text-[11px] text-ink-faint">{i + 1}</span>
            <select
              value={c.date}
              aria-label={`Preference ${i + 1} date`}
              onChange={(e) =>
                setChoices((p) => p.map((x, j) => (j === i ? { ...x, date: e.target.value } : x)))
              }
              className={input}
            >
              {dates.map((d) => (
                <option key={d} value={d}>
                  {formatDate(d)}
                </option>
              ))}
            </select>
            <select
              value={c.window}
              aria-label={`Preference ${i + 1} window`}
              onChange={(e) =>
                setChoices((p) =>
                  p.map((x, j) => (j === i ? { ...x, window: Number(e.target.value) } : x)),
                )
              }
              className={input}
            >
              {WINDOWS.map((w, idx) => (
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
        I&apos;d take an earlier slot if one frees up
      </label>

      <Button type="submit" variant="primary" busy={busy} className="w-full">
        Send
      </Button>
    </form>
  );
}

const input =
  "w-full rounded-[8px] border border-rail-strong bg-surface px-2.5 py-1.5 text-[13px] text-ink " +
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
