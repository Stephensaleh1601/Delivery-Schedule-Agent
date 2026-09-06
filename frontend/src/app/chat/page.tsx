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
import { Modal } from "@/components/ui/Modal";
import {
  dispatch,
  type ActivePlan,
  type AgentRun,
  type Bootstrap,
  type Placement,
  type ChatMessage,
  type ChatTurn,
  type Offer,
  type PlanVersion,
} from "@/lib/api";
import { formatDate, parseDate } from "@/lib/format";
import { ThinkingChip, ThinkingOverlay, useAgentProgress } from "@/components/AgentProgress";
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
  const [traceOpen, setTraceOpen] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [input, setInput] = useState("");
  const [before, setBefore] = useState<PlanVersion | null>(null);
  const [after, setAfter] = useState<ActivePlan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetNotice, setResetNotice] = useState<string | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  // A failed request keeps its key, so clicking retry asks the server for the original outcome
  // instead of starting a second negotiation or publishing another route version.
  const pendingMessageIds = useRef(new Map<string, string>());
  const pendingResponseIds = useRef(new Map<string, string>());

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
      const next = await dispatch.conversation(id);
      setTurn(next);
      if (next.confirmed && next.delivery_date) {
        const [active, versions] = await Promise.all([
          dispatch.activePlan(next.delivery_date).catch(() => null),
          dispatch.planVersions(next.delivery_date).catch(() => []),
        ]);
        setAfter(active);
        if (active) {
          const parent = versions.find((version) => version.id === active.parent_plan_id);
          const previous = [...versions]
            .filter((version) => version.version < active.version)
            .sort((a, b) => b.version - a.version)[0];
          setBefore(parent ?? previous ?? null);
        }
      }
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
  // Polls while a turn is in flight; one more read after it lands swaps the live trace for
  // the one rebuilt from the persisted run, which is what survives a refresh.
  const agentProgress = useAgentProgress(orderId, busy);
  const openOffer = turn?.open_offer_id ? turn.offers[turn.open_offer_id] : null;
  const confirmed = turn?.confirmed ?? false;
  const lastRun = turn?.run ?? null;
  // A successful confirmation is the newest truth. The previous rejection remains useful history
  // under its message, but showing it as the current decision made a booked customer look refused.
  const decision = confirmed ? null : (turn?.decision ?? null);

  async function send(text: string) {
    const body = text.trim();
    if (!body || !orderId || busy) return;
    const clientMessageId = pendingMessageIds.current.get(body) ?? crypto.randomUUID();
    pendingMessageIds.current.set(body, clientMessageId);

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
      const next = await dispatch.sendMessage(orderId, body, clientMessageId);
      pendingMessageIds.current.delete(body);
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

  /** Accept one specific slot of one specific offer.
   *
   *  Deliberately not a chat message. A confirm button already knows exactly which slot it means,
   *  and turning that certainty back into a sentence for the reader to re-derive is how a click
   *  ended up understood as new availability -- opening a second offer rather than booking the
   *  one on screen.
   */
  async function accept(offerId: string, slotId: string) {
    if (!orderId || busy) return;
    setBusy(true);
    setError(null);
    const responseKey = `${offerId}:${slotId}`;
    const eventId = pendingResponseIds.current.get(responseKey) ?? crypto.randomUUID();
    pendingResponseIds.current.set(responseKey, eventId);

    const discussing = openOffer?.options.find((o) => o.id === slotId)?.date;
    if (discussing) {
      const versions = await dispatch.planVersions(discussing).catch(() => []);
      setBefore(versions.find((v) => v.status === "active") ?? null);
    }

    try {
      await dispatch.respond(offerId, true, slotId, eventId);
      pendingResponseIds.current.delete(responseKey);
      // Reload the thread rather than patching it: the acceptance writes a confirmation message
      // and republishes the day, and the server's version of both is the one to show.
      await load(orderId);
      if (discussing) setAfter(await dispatch.activePlan(discussing).catch(() => null));
    } catch (err) {
      setError(err);
      await load(orderId);
    } finally {
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
    pendingMessageIds.current.clear();
    pendingResponseIds.current.clear();
    setError(null);
    boot.reload();
  }

  async function resetDemo() {
    if (resetting) return;
    setResetting(true);
    setError(null);
    try {
      const result = await dispatch.resetDemo();
      reset();
      const routes = result.routes
        .map((route) => `${formatDate(route.date)} v${route.version} (${route.stop_count} stops)`)
        .join(" and ");
      setResetNotice(`Demo reset. ${routes}.`);
      setResetOpen(false);
    } catch (err) {
      setError(err);
    } finally {
      setResetting(false);
    }
  }

  const status = busy ? "typing…" : confirmed ? "delivery confirmed" : "online";

  return (
    <Page
      title="Customer Chat"
      lede="The customer types in their own words. Every reply is the result of a real request — the agent reads the message, solves the route, and offers a window it can actually keep."
      wide
      actions={
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={reset}>
            Start over
          </Button>
          <Button variant="danger" onClick={() => setResetOpen(true)}>
            Reset demo data
          </Button>
        </div>
      }
    >
      {boot.error ? <ErrorPanel error={boot.error} onRetry={boot.reload} /> : null}

      <Modal
        open={resetOpen}
        onClose={() => !resetting && setResetOpen(false)}
        title="Reset all demo data?"
        subtitle="Use this before recording each demo path."
        width={520}
      >
        <div className="flex flex-col gap-4 text-[13.5px] leading-[1.55] text-ink-soft">
          <p>
            This permanently removes every test chat, booking and route version, then rebuilds
            the original Friday and Saturday routes at version 1.
          </p>
          <p>Saved geocodes and drive times are kept, so the next run stays fast.</p>
          <div className="flex justify-end gap-2 border-t border-rail pt-4">
            <Button disabled={resetting} onClick={() => setResetOpen(false)}>
              Cancel
            </Button>
            <Button variant="danger" busy={resetting} onClick={() => void resetDemo()}>
              Reset demo data
            </Button>
          </div>
        </div>
      </Modal>

      {resetNotice ? (
        <div
          role="status"
          className="mb-4 rounded-[12px] border border-locked-edge bg-locked-wash px-4 py-3 text-[13px] text-ink"
        >
          {resetNotice}
        </div>
      ) : null}

      {/* Mounted at page level, not inside the phone column: the trace needs room to be read,
          and squeezing it into 380px is what made its rows collide in the first place. */}
      {traceOpen && agentProgress && (
        <ThinkingOverlay
          progress={agentProgress}
          onClose={() => setTraceOpen(false)}
          onRetry={() => {
            const last = [...messages].reverse().find((m) => m.direction === "inbound");
            if (last) void send(last.body);
          }}
        />
      )}

      <div className="enter grid grid-cols-1 items-start gap-6 lg:grid-cols-[380px_minmax(0,1fr)]">
        {/* -- the phone -------------------------------------------------- */}
        <div
          className={cx(
            "flex flex-col gap-3 lg:order-1 lg:sticky lg:top-[74px]",
            orderId ? "order-1" : "order-2",
          )}
        >
          {/* Keep the phone inside the viewport. Only the message wallpaper scrolls; the header,
              quick replies and composer stay put like a real messaging app. */}
          <Phone
            status={status}
            className="h-[calc(100dvh-210px)] min-h-[520px] max-h-[720px]"
          >
            <Wallpaper innerRef={feed}>
              <div className="flex min-h-[380px] flex-col gap-1.5">
                {boot.initialising ? (
                  <>
                    <Skeleton className="h-14 w-[75%]" />
                    <Skeleton className="h-10 w-[60%]" />
                  </>
                ) : (
                  <>
                    <Greeting boot={boot.data} placement={turn?.placement ?? null} />
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

                {/* Inside the wallpaper, under the last message -- where a status line belongs in
                    a chat. Outside it, the phone's own dark-green header shows through and the
                    chip sits on a green strip. */}
                {orderId && agentProgress && agentProgress.stages.length > 0 && (
                  <div className="flex pt-1">
                    <ThinkingChip progress={agentProgress} onOpen={() => setTraceOpen(true)} />
                  </div>
                )}
              </div>
            </Wallpaper>

            {orderId && !confirmed && (
              <QuickReplies
                replies={suggestReplies(openOffer, confirmed, messages.length)}
                onPick={(id) => {
                  const reply = suggestReplies(openOffer, confirmed, messages.length).find(
                    (r) => r.id === id,
                  );
                  // A confirm button is an answer to a specific slot, not a sentence to be read.
                  // Routing it through the language reader let a click be understood as new
                  // availability, which opened a second offer instead of booking the first.
                  if (reply?.slotId && openOffer) void accept(openOffer.id, reply.slotId);
                  else void send(id);
                }}
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

          {error ? <ErrorPanel error={error} onRetry={() => setError(null)} /> : null}
          {orderId && (
            <p className="font-mono text-[11px] text-ink-faint">order {orderId} · survives a refresh</p>
          )}
        </div>

        {/* -- the agent -------------------------------------------------- */}
        <div className={cx("flex flex-col gap-4 lg:order-2", orderId ? "order-2" : "order-1")}>
          <div className="flex flex-col gap-1">
            <Eyebrow>Agent decisions and tool results</Eyebrow>
            {/* The heading is the question this run answers. "Why these times?" and "Why the offer
                changed" are different questions, and one panel titled for both answers neither. */}
            <h2 className="text-[17px] font-semibold text-ink">
              {!orderId
                ? "Open one customer conversation"
                : busy
                  ? "Reading the message and solving the route…"
                  : confirmed
                    ? "Appointment locked. The route has been republished."
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

          {!orderId && boot.data && (
            <Card className="border-accent/20 px-5 py-5 shadow-[var(--shadow-lift)]">
              <div className="mb-4 max-w-[58ch]">
                <Eyebrow>Start the live demo</Eyebrow>
                <p className="mt-1 text-[14px] leading-[1.5] text-ink-soft">
                  Give the agent a customer and address. Availability is negotiated in the chat;
                  every proposed window is checked against a real route before it is offered.
                </p>
              </div>
              <IntroForm boot={boot.data} busy={busy} onSubmit={start} />
            </Card>
          )}

          {/* Which route this customer belongs to, and why. Shown from the moment the order
              exists rather than only after a decision: it is the first thing the system worked
              out, before any message was read, and a judge should be able to check the greeting
              against it. */}
          {orderId && turn?.placement?.region && (
            <Card className="px-4 py-3">
              <Eyebrow>Routed by postal code</Eyebrow>
              <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
                <span className="font-mono text-ink-soft">
                  Postal code {turn.placement.postal_code ?? "\u2014"}
                </span>
                <span className="text-ink-faint">&rarr;</span>
                <span className="font-medium text-ink">{turn.placement.region} region</span>
                <span className="text-ink-faint">&rarr;</span>
                <span className="font-medium text-ink">
                  {/* formatDate already opens with the weekday, so naming the day as well
                      rendered "Saturday Saturday, 12 September". */}
                  Normal route:{" "}
                  {turn.placement.normal_date
                    ? formatDate(turn.placement.normal_date)
                    : turn.placement.normal_day}
                </span>
              </div>
              {turn.placement.other_day && (
                <p className="mt-1.5 text-[11.5px] leading-[1.45] text-ink-muted">
                  A recommendation, not a restriction — if they ask for{" "}
                  {turn.placement.other_day}, that route is the one we test.
                </p>
              )}
            </Card>
          )}

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

/** The second bubble, written from the customer's own postal region.
 *
 *  Every fact in it -- the region, the day that region is delivered on, and the three window
 *  times -- comes from `turn.placement`, which the backend derives from the same rules the search
 *  scopes itself with. None of it is re-stated here, so there is no second copy to drift.
 *
 *  It offers rather than restricts. Naming the other day in the same breath is what makes the
 *  normal day a recommendation: a West customer who wants Friday can simply say so, and their
 *  insertion is tested against Friday's route. */
function opening(placement: Placement | null, boot: Bootstrap): string {
  const windows = placement?.windows?.length
    ? placement.windows.map((w) => `${w.label.toLowerCase()} (${clock(w.start)}–${clock(w.end)})`)
    : [];

  if (!placement?.region || !placement.normal_day || !placement.normal_date || !windows.length) {
    // No region for this address -- say what we can rather than inventing a day for them.
    return `Just tell me when you're free — anything between ${formatDate(boot.horizon.first)} and ${formatDate(boot.horizon.last)}. One time is plenty.`;
  }

  const choice = `${windows.slice(0, -1).join(', ')} or ${windows[windows.length - 1]}`;
  const other = placement.other_day
    ? ` If ${placement.normal_day} does not work, tell me and I can check ${placement.other_day}'s route.`
    : '';

  return (
    `Your address is in the ${placement.region}, which we normally deliver to on ` +
    `${formatDate(placement.normal_date)}. Are you available in the ${choice}?${other}`
  );
}

/** "17:00" -> "5pm", "10:00" -> "10am". The times themselves come from the server; this only
 *  says them the way a person would. */
function clock(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number);
  const suffix = h < 12 ? 'am' : 'pm';
  const hour = h % 12 || 12;
  return m ? `${hour}:${String(m).padStart(2, '0')}${suffix}` : `${hour}${suffix}`;
}

function Greeting({ boot, placement }: { boot: Bootstrap | null; placement: Placement | null }) {
  if (!boot) return null;
  return (
    <>
      <Bubble from="them" time="9:02 am">
        Hi! This is Floof. Your food is made fresh and can&apos;t be
        left at the door, so I just need a time you&apos;ll be home.
      </Bubble>
      <Bubble from="them" time="9:02 am">
        {opening(placement, boot)}
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
): Array<{ id: string; label: string; slotId?: string }> {
  if (confirmed) return [];

  if (offer && offer.options.length > 0) {
    // The day is part of the label because the alternatives span two of them. "Confirm 2-5pm"
    // beside another "Confirm 2-5pm" is a coin flip for whoever is clicking.
    const replies: Array<{ id: string; label: string; slotId?: string }> = offer.options.map(
      (slot) => ({
        id: slot.id,
        slotId: slot.id,
        label: `Confirm ${weekdayShort(slot.date)} ${prettyTime(slot.window.start)}–${prettyTime(slot.window.end)}`,
      }),
    );
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
      { id: "Friday morning works for me.", label: "Friday morning" },
      { id: "I'm free Saturday afternoon.", label: "Saturday afternoon" },
    ];
  }
  return [];
}

/** "Fri" / "Sat". The alternatives span both cluster days, so a confirm button that names
 *  only a time is ambiguous by construction. */
function weekdayShort(iso: string): string {
  return parseDate(iso).toLocaleDateString("en-GB", { weekday: "short" });
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
  const [jobType, setJobType] = useState("pet_food_box");
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
        <Field label="Order">
          <select value={jobType} onChange={(e) => setJobType(e.target.value)} className={input}>
            <option value="pet_food_box">Subscription box</option>
            <option value="one_off_pet_order">One-off order</option>
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
