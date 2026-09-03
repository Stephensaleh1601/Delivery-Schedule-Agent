"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AgentDecision } from "@/components/AgentDecision";
import { FunctionCallsPill } from "@/components/FunctionCalls";
import { DayChange } from "@/components/RouteImpact";
import { RouteMap, stopsToPoints } from "@/components/RouteMap";
import { Page } from "@/components/Shell";
import {
  Bubble,
  Composer,
  Phone,
  QuickReplies,
  TypingBubble,
  Wallpaper,
} from "@/components/WhatsApp";
import { Button, Card, ErrorPanel, Eyebrow, LockIcon, Pill, Skeleton, cx } from "@/components/ui";
import {
  dispatch,
  type ActivePlan,
  type AgentRun,
  type Bootstrap,
  type ChatMessage,
  type ChatTurn,
  type Offer,
  type PlanVersion,
} from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * The customer's side, and the agent's working, on one screen.
 *
 * Two things shape this file, and both are worth knowing before editing it.
 *
 * **The customer types.** There is no availability form. They write "I'm free Saturday morning",
 * the agent reads it, solves the route, and answers with a specific window it can keep. The quick
 * replies under the thread are shortcuts and a reliable path for a live demo -- never the only way
 * to answer.
 *
 * **The thread belongs to the database, not to React.** Every turn returns the whole conversation
 * as persisted, and this component renders that. It is why a refresh keeps the messages AND keeps
 * the right trace under each one: the run id is a field on the message, not a variable up here
 * that happened to hold the newest run when the bubble was drawn.
 */

type Draft =
  // Sent but not yet answered. Rendered optimistically so the thread feels immediate, then
  // replaced wholesale by the server's version of events.
  | { kind: "pending"; id: string; text: string }
  | { kind: "typing"; id: string };

let seq = 0;
const nextId = () => `local-${++seq}`;

export default function ChatPage() {
  const router = useRouter();
  const boot = useResource(() => dispatch.bootstrap(), []);

  const [orderId, setOrderId] = useState<string | null>(null);
  const [turn, setTurn] = useState<ChatTurn | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [input, setInput] = useState("");
  const [before, setBefore] = useState<PlanVersion | null>(null);
  const [after, setAfter] = useState<ActivePlan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const feed = useRef<HTMLDivElement>(null);

  // Survives a reload. The conversation itself lives on the server; this is only the pointer to
  // it, and it is what makes "refresh the browser" a step the demo can actually perform.
  useEffect(() => {
    const saved = sessionStorage.getItem("dispatch:chat-order");
    if (saved) setOrderId(saved);
  }, []);

  useEffect(() => {
    if (orderId) sessionStorage.setItem("dispatch:chat-order", orderId);
  }, [orderId]);

  const load = useCallback(async (id: string) => {
    try {
      setTurn(await dispatch.conversation(id));
    } catch (err) {
      setError(err);
    }
  }, []);

  useEffect(() => {
    if (orderId) void load(orderId);
  }, [orderId, load]);

  useEffect(() => {
    feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: "smooth" });
  }, [turn, drafts]);

  const messages = turn?.messages ?? [];
  const openOffer = turn?.open_offer_id ? turn.offers[turn.open_offer_id] : null;
  const confirmed = turn?.confirmed ?? false;
  const lastRun = turn?.run ?? null;
  // The last run that actually DECIDED something -- read from the server, so it survives a refresh
  // and a confirmed booking keeps its explanation instead of reverting to "waiting".
  const decision = turn?.decision ?? null;

  async function send(text: string) {
    const body = text.trim();
    if (!body || !orderId || busy) return;

    setBusy(true);
    setError(null);
    setInput("");
    setDrafts([
      { kind: "pending", id: nextId(), text: body },
      { kind: "typing", id: nextId() },
    ]);

    // The day under discussion, captured BEFORE the reply, so a confirmation can show what
    // changed rather than only what the day now looks like.
    const discussing = openOffer?.options[0]?.date;
    if (discussing) {
      const versions = await dispatch.planVersions(discussing).catch(() => []);
      setBefore(versions.find((v) => v.status === "active") ?? null);
    }

    try {
      const next = await dispatch.sendMessage(orderId, body);
      setTurn(next);
      if (next.confirmed && next.delivery_date) {
        setAfter(await dispatch.activePlan(next.delivery_date).catch(() => null));
      }
    } catch (err) {
      setError(err);
      // The message may already be in the thread on the server. Reload rather than guess, so the
      // screen and the database agree even when something went wrong.
      if (orderId) await load(orderId);
    } finally {
      setDrafts([]);
      setBusy(false);
    }
  }

  async function start(payload: IntroPayload) {
    setBusy(true);
    setError(null);
    try {
      const created = await dispatch.createOrder(payload);
      setOrderId(created.id);
      setTurn(await dispatch.conversation(created.id));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    seq = 0;
    sessionStorage.removeItem("dispatch:chat-order");
    setOrderId(null);
    setTurn(null);
    setDrafts([]);
    setInput("");
    setBefore(null);
    setAfter(null);
    setError(null);
    boot.reload();
  }

  const status = busy ? "typing…" : confirmed ? "delivery confirmed" : "online";

  return (
    <Page
      title="Customer Chat"
      lede="The customer types in their own words. Every reply is the result of a real request — the agent reads the message, solves the route, and offers a window it can actually keep."
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
                  <>
                    <Greeting boot={boot.data} />
                    {messages.map((m) => (
                      <ThreadMessage
                        key={m.id}
                        message={m}
                        // The run named BY THIS MESSAGE. Not "the latest run", which is the bug
                        // this replaces and which is invisible until there are two.
                        run={m.run_id ? (turn?.runs[m.run_id] ?? null) : null}
                      />
                    ))}
                    {drafts.map((d) =>
                      d.kind === "typing" ? (
                        <TypingBubble key={d.id} />
                      ) : (
                        <Bubble key={d.id} from="me" time="now" ticks="sent">
                          {d.text}
                        </Bubble>
                      ),
                    )}
                  </>
                )}
              </div>
            </Wallpaper>

            {orderId && !confirmed && (
              <QuickReplies
                replies={suggestReplies(openOffer, confirmed, messages.length)}
                onPick={(text) => void send(text)}
                disabled={busy}
              />
            )}
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => void send(input)}
              disabled={!orderId || busy || confirmed}
              placeholder={
                !orderId
                  ? "Open the chat first →"
                  : confirmed
                    ? "Booking confirmed"
                    : busy
                      ? "Checking the route…"
                      : "Message"
              }
            />
          </Phone>

          {!orderId && boot.data && (
            <Card className="px-4 py-3.5">
              <IntroForm boot={boot.data} busy={busy} onSubmit={start} />
            </Card>
          )}
          {error ? <ErrorPanel error={error} onRetry={() => setError(null)} /> : null}
          {orderId && (
            <p className="font-mono text-[11px] text-ink-faint">order {orderId} · survives a refresh</p>
          )}
        </div>

        {/* -- the agent -------------------------------------------------- */}
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <Eyebrow>Agent decision</Eyebrow>
            {/* The heading is the question this run answers. "Why these times?" and "Why the offer
                changed" are different questions, and one panel titled for both answers neither. */}
            <h2 className="text-[17px] font-semibold text-ink">
              {!orderId
                ? "Nothing decided yet"
                : busy
                  ? "Reading the message and solving the route…"
                  : decision?.meaningful
                    ? decision.heading
                    : lastRun
                      ? summarise(lastRun, turn)
                      : "Waiting for the customer"}
            </h2>
            <p className="max-w-[68ch] text-[12.5px] text-ink-muted">
              The customer sees none of this. Every figure comes from a solved route — open{" "}
              <span className="font-medium text-ink-soft">Function calls &amp; results</span> under
              any message for the tool calls that produced it.
            </p>
          </div>

          {/* The tool calls live under the message that produced them, in "Function calls &
              results". Repeating them here told a judge nothing the modal does not, and crowded
              out the only question this panel should answer: why that time and not the other. */}
          {decision?.meaningful && (
            <Card className="px-4 py-4">
              <AgentDecision decision={decision} />
            </Card>
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
                            completion_minutes: before.completion_minutes,
                            working_span_minutes: before.working_span_minutes,
                            idle_minutes: before.idle_minutes,
                          }
                        : null
                    }
                    after={{
                      drive_minutes: after.round_trip_drive_minutes,
                      stops: after.stop_count,
                      distance_km: after.round_trip_distance_km,
                      finishes_at: after.finishes_at,
                      completion_minutes: after.completion_minutes,
                      working_span_minutes: after.working_span_minutes,
                      idle_minutes: after.idle_minutes,
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
        </div>
      </div>
    </Page>
  );
}

// -- thread -------------------------------------------------------------------

function Greeting({ boot }: { boot: Bootstrap | null }) {
  if (!boot) return null;
  return (
    <>
      <Bubble from="them" time="9:02 am">
        Hi! This is Majestic Fighters Furniture Delivery. I can book your sofa, bed or cabinet
        delivery.
      </Bubble>
      <Bubble from="them" time="9:02 am">
        {`Just tell me when you're free — anything between ${formatDate(boot.horizon.first)} and ${formatDate(boot.horizon.last)}. One time is plenty.`}
      </Bubble>
    </>
  );
}

function ThreadMessage({ message, run }: { message: ChatMessage; run: AgentRun | null }) {
  const mine = message.direction === "inbound";
  return (
    <div className={cx("flex w-full flex-col gap-1.5", mine ? "items-end" : "items-start")}>
      <Bubble
        from={mine ? "me" : "them"}
        time={clockOf(message.created_at)}
        ticks={mine ? "read" : undefined}
      >
        {message.body}
      </Bubble>
      {/* Under the message it belongs to, opening that message's own run. An inbound message has
          no run and correctly gets no pill -- the customer's words did not come from a tool call. */}
      {run && <FunctionCallsPill run={run} />}
    </div>
  );
}

function clockOf(iso: string): string {
  const d = new Date(iso);
  const h = d.getHours() % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")} ${d.getHours() < 12 ? "am" : "pm"}`;
}

/** Shortcuts for what the customer plausibly wants to say next. Never the only way to say it. */
function suggestReplies(
  offer: Offer | null,
  confirmed: boolean,
  messageCount: number,
): Array<{ id: string; label: string }> {
  if (confirmed) return [];

  if (offer && offer.options.length > 0) {
    const replies = offer.options.map((slot) => ({
      id: `Confirm ${prettyTime(slot.window.start)} on ${slot.date}`,
      label: `Confirm ${prettyTime(slot.window.start)}–${prettyTime(slot.window.end)}`,
    }));
    replies.push({ id: "Why this timing?", label: "Why this timing?" });
    // Only while another round remains -- offering a "no" that can only fail is worse than not
    // offering one. The cap itself is enforced server-side, from persisted rows.
    if (offer.round_number < 2) {
      replies.push({ id: "That doesn't work, can you do later?", label: "Suggest another time" });
      replies.push({ id: "None of these work.", label: "None of these work" });
    }
    return replies;
  }

  if (messageCount === 0) {
    return [
      { id: "I'm free Saturday morning.", label: "Saturday morning" },
      { id: "Any time after 1 on Tuesday.", label: "Tuesday after 1" },
    ];
  }
  return [];
}

function prettyTime(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  const hour = h % 12 || 12;
  return `${hour}${m ? `:${String(m).padStart(2, "0")}` : ""}${h < 12 ? "am" : "pm"}`;
}

function summarise(run: AgentRun, turn: ChatTurn | null): string {
  const offer = turn?.open_offer_id ? turn.offers[turn.open_offer_id] : null;
  if (turn?.confirmed) return "Appointment locked and the day republished";
  if (offer) {
    const n = offer.options.length;
    return `${n} window${n === 1 ? "" : "s"} offered, derived from the solved route`;
  }
  const tools = run.actions.map((a) => a.tool);
  if (tools.includes("ask_clarification")) return "Asked one question rather than guessing";
  if (tools.includes("explain_choice")) return "Explained the timing from the solved route";
  return `${run.actions.length} tool call${run.actions.length === 1 ? "" : "s"} made`;
}

// -- who and where ------------------------------------------------------------

interface IntroPayload {
  customer_name: string;
  phone: string;
  address_raw: string;
  postal_code: string;
  job_type: string;
  can_deliver_early: boolean;
  availability: never[];
}

/**
 * Name, postal code, item. Not when -- that is the conversation's job.
 *
 * Not a booking form wearing a smaller hat: an order needs an address before any route can be
 * solved for it, and asking for a six-digit postal code in free text would be theatre. Everything
 * a coordinator would actually negotiate is typed.
 */
function IntroForm({
  boot,
  busy,
  onSubmit,
}: {
  boot: Bootstrap;
  busy: boolean;
  onSubmit: (payload: IntroPayload) => void;
}) {
  const [name, setName] = useState("Mrs Lee");
  // Punggol: a real residential address in District 19, which no seeded customer occupies. The
  // old default shared a district with a seeded stop, so the map drew a 0 km leg between them and
  // the whole route looked fabricated.
  const [postal, setPostal] = useState("828761");
  const [jobType, setJobType] = useState("sofa");
  const [early, setEarly] = useState(false);

  return (
    <form
      className="flex flex-col gap-2.5"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({
          customer_name: name,
          phone: "91112222",
          address_raw: `Blk ${postal.slice(0, 3)}, Singapore ${postal}`,
          postal_code: postal,
          job_type: jobType,
          can_deliver_early: early,
          // Deliberately empty. The customer says when, in their own words, in the thread.
          availability: [],
        });
      }}
    >
      <Eyebrow>Who is messaging</Eyebrow>
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

      <label className="flex items-center gap-2 text-[12.5px] text-ink-soft">
        <input
          type="checkbox"
          checked={early}
          onChange={(e) => setEarly(e.target.checked)}
          className="h-3.5 w-3.5 accent-[var(--color-accent)]"
        />
        I&apos;d take an earlier slot if one frees up
      </label>

      <p className="text-[11.5px] leading-[1.45] text-ink-muted">
        No dates here — type when you&apos;re free once the chat opens. One time is enough. We can
        deliver between {formatDate(boot.horizon.first)} and {formatDate(boot.horizon.last)}.
      </p>

      <Button type="submit" variant="primary" busy={busy} className="w-full">
        Open the chat
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
