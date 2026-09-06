"use client";

/**
 * The deck, as a page.
 *
 * Seven slides, read top to bottom, one idea each. The audience is someone meeting this for the
 * first time -- a judge, a new coordinator, the client -- so every slide leads with a picture and
 * says the least that makes the picture make sense.
 *
 * Each slide carries the judging criterion it answers. That is not decoration: the brief scores
 * five things at 20% each, and a deck where nobody can tell which slide argues "originality" has
 * left points on the table. The tags also keep the copy honest -- a slide that cannot name its
 * criterion is a slide that is not doing work.
 *
 * Source of truth is `slides.md` at the repo root; this renders the same seven. Move one, move both.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { Eyebrow, Pill, cx } from "@/components/ui";
import {
  AgentArchitecture,
  BeforeAfter,
  CheckList,
  DetourComparison,
  Figure,
  Flow,
  RouteLine,
} from "@/components/about/parts";

const SLIDES = [
  "The ask",
  "The problem",
  "What we built",
  "How it is built",
  "Fitting people in",
  "The prototype",
  "What changes",
];

export default function AboutPage() {
  const active = useActiveSlide(SLIDES.length);

  return (
    <div className="mx-auto flex max-w-[1180px] gap-10 px-7 py-10">
      <Rail active={active} />

      <div className="flex min-w-0 flex-1 flex-col">
        {/* 01 ------------------------------------------------------------- */}
        <Slide n={0} eyebrow="The ask" criterion="Presentation" first>
          <Headline>Nobody should spend an afternoon asking forty people when they are home</Headline>
          <Lede>
            An AI delivery coordinator that agrees a time by text, checks the published route and
            only offers windows the van can keep.
          </Lede>

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Pill tone="accent">Floof.sg — fresh pet food, Singapore</Pill>
            <Pill>IGNITE Agentic AI Hackathon 2026</Pill>
            <Pill>Team Majestic Fighters</Pill>
          </div>

          <p className="text-[17px] leading-[1.6] text-ink-muted">
            The three tabs beside this one are the working product.{" "}
            <Link href="/chat" className="text-accent underline underline-offset-2">
              Customer Chat
            </Link>{" "}
            is where you can watch it think.
          </p>
        </Slide>

        {/* 02 ------------------------------------------------------------- */}
        <Slide n={1} eyebrow="The problem" criterion="Benefits · Presentation">
          <Headline>Two hard jobs, both done by hand</Headline>
          <Lede>
            Floof.sg coordinates attended deliveries for fresh pet food. <Strong>Somebody has to
            be home.</Strong> Every order needs a time the customer agreed to.
          </Lede>

          <Segment label="Job 1 — asking everybody">
            <Figure>
              <Flow
                steps={[
                  { title: "30–40 deliveries", note: "on a busy day" },
                  {
                    title: "One coordinator",
                    note: "asking on WhatsApp",
                    icon: <WhatsAppMark />,
                  },
                  { title: "One “no”", note: "starts another route search" },
                ]}
              />
            </Figure>
            <Note title="The bottleneck">
              Customer availability becomes route input. One “no” starts another route search.
            </Note>
          </Segment>

          <Segment label="Job 2 — deciding who to visit first">
            <Figure caption="Same five customers, same van, same day. Only the order changed.">
              <DetourComparison />
            </Figure>
            <p className="text-[17px] leading-[1.6] text-ink-soft">
              Detours turn into late deliveries. A person works this out by hand — and drivers still
              change it on the road, so the plan and the day drift apart.
            </p>
          </Segment>

          <Statement>
            The hard part is turning changing customer answers into promises the route can
            actually keep.
          </Statement>
        </Slide>

        {/* 03 ------------------------------------------------------------- */}
        <Slide n={2} eyebrow="What we built" criterion="Effectiveness">
          <Headline>The customer texts. The helper replies with a time it can keep.</Headline>

          <Figure caption="No forms. No “send us three options”. One message, one answer.">
            <Flow
              steps={[
                { title: "“I'm free Saturday morning”", note: "their own words" },
                { title: "Read it", note: "what do they mean?" },
                { title: "Check the real route", note: "where would they fit?" },
                { title: "Offer one time", note: "one the van can keep" },
              ]}
            />
          </Figure>

          <Segment label="The two customers in our demo">
            <div className="grid gap-3 sm:grid-cols-2">
              <PersonCard
                name="Mrs Chua"
                tone="locked"
                does="Says yes to the first time offered."
                agent="One search, one offer, booked."
              />
              <PersonCard
                name="Mr Rajan"
                tone="pending"
                does="Rejects the first offer."
                agent="Looks wider, offers three, or hands it to a person."
              />
            </div>
            <p className="text-[16px] leading-[1.55] text-ink-muted">
              The easy path takes one search and one confirmation. The difficult path adapts to a
              rejection without guessing or moving an existing booking.
            </p>
          </Segment>
        </Slide>

        {/* 04 ------------------------------------------------------------- */}
        <Slide n={3} eyebrow="How it is built" criterion="Innovation · Technical quality">
          <Headline>Everything comes back to the same question</Headline>
          <Lede>
            Each box is marked <Tag who="ai" /> or <Tag who="code" />. The AI appears twice — and
            neither time does it touch a number.
          </Lede>

          <Figure>
            <AgentArchitecture />
          </Figure>

          <div className="grid gap-3 sm:grid-cols-2">
            <Note title="Six actions, not sixty.">
              Each is a whole job, the way a person would think about it. A long list of small steps
              is a long list of chances to pick the wrong one.
            </Note>
            <Note title="It can’t search the wrong day.">
              Which days an action may read is built into that action, not typed in by the AI.
              “Their usual day” can only ever see one day.
            </Note>
          </div>
        </Slide>

        {/* 05 ------------------------------------------------------------- */}
        <Slide n={4} eyebrow="Fitting people in" criterion="Innovation · Technical quality">
          <Headline>Slide them into the day. Don&rsquo;t rebuild it.</Headline>
          <Lede>
            Our seeded demo uses two delivery days, each covering one part of Singapore. An
            address tells the agent which published route to check first.
          </Lede>

          <div className="grid gap-3 sm:grid-cols-2">
            <DayCard day="Friday" regions="North · North-East · South · East" />
            <DayCard day="Saturday" regions="Central · City · West" />
          </div>

          <Figure caption="We try them before and after each nearby stop, then check everyone after them still arrives on time.">
            <RouteLine
              label="Friday's van, already planned"
              stops={["Chen Li Hua", "Marcus Tan", "Priya Nair"]}
              insertAt={1}
            />
          </Figure>

          <Segment label="Four rules that never bend">
            <CheckList
              items={[
                {
                  ok: false,
                  text: (
                    <>
                      <Strong>Never shuffle.</Strong> Everyone already booked stays put.
                    </>
                  ),
                },
                {
                  ok: false,
                  text: (
                    <>
                      <Strong>Never make anyone late.</Strong> Every later stop is re-checked, not
                      assumed.
                    </>
                  ),
                },
                {
                  ok: false,
                  text: (
                    <>
                      <Strong>Never add a delivery day.</Strong> It only fills days the van already
                      drives.
                    </>
                  ),
                },
                {
                  ok: true,
                  text: (
                    <>
                      <Strong>One offer, or three.</Strong> One good time; three only after a “no”.
                    </>
                  ),
                },
              ]}
            />
          </Segment>

          <Note title="Wide windows, honestly">
            Customers get <Strong>morning 10–2</Strong>, <Strong>afternoon 2–5</Strong> or{" "}
            <Strong>evening 5–9</Strong> — what the business can genuinely promise. A fake
            15-minute slot breaks on the first traffic jam.
          </Note>
        </Slide>

        {/* 06 ------------------------------------------------------------- */}
        <Slide n={5} eyebrow="The prototype" criterion="Effectiveness · Technical quality">
          <Headline>It runs. Here is where.</Headline>

          <div className="grid gap-3 sm:grid-cols-3">
            <ScreenCard href="/" name="Orders" text="Every order, and where it has got to." />
            <ScreenCard href="/routes" name="Daily Routes" text="Both days, mapped, in visiting order." />
            <ScreenCard href="/chat" name="Customer Chat" text="The conversation, and what the helper did." />
          </div>

          <Segment label="While the customer waits">
            <Figure caption="Each line is written by the code that did that thing, as it did it. Nothing is faked to look busy.">
              <ol className="flex flex-col gap-1.5">
                {[
                  ["Read the customer’s message", "done"],
                  ["Checked the published route", "done"],
                  ["Offered 2–5pm with route evidence", "sent"],
                ].map(([text, time]) => (
                  <li
                    key={text}
                    className="flex items-center justify-between gap-4 rounded-[9px] border border-rail bg-sunk/50 px-3.5 py-2.5"
                  >
                    <span className="flex items-center gap-2.5 text-[16px] text-ink">
                      <TickIcon />
                      {text}
                    </span>
                    <span className="font-mono text-[13.5px] tnum text-ink-faint">{time}</span>
                  </li>
                ))}
              </ol>
            </Figure>
          </Segment>

          <Segment label="Integration path">
            <div className="grid gap-3 sm:grid-cols-2">
              <Note title="Working now">
                The agent loop, route maths, policy rules, operations console and database. Python,
                FastAPI, Next.js, OR-Tools, LangGraph and Claude on AWS Bedrock.
              </Note>
              <Note title="Connect to the business">
                The simulated customer thread and demo address lookup sit behind clear interfaces,
                ready to connect to the operator&rsquo;s messaging and address systems.
              </Note>
            </div>
          </Segment>
        </Slide>

        {/* 07 ------------------------------------------------------------- */}
        <Slide n={6} eyebrow="What changes" criterion="Benefits" last>
          <Headline>An afternoon of texting becomes something that answers itself</Headline>

          <BeforeAfter
            rows={[
              { before: "One coordinator chasing dozens of replies", after: "Each customer answered as they reply" },
              { before: "Drivers sorting out timings between stops", after: "Drivers get a finished route the day before" },
              { before: "“Not Friday” means starting over", after: "A “no” searches somewhere else by itself" },
              { before: "The plan lives in one person's head", after: "Every decision can be looked up afterwards" },
            ]}
          />

          <Segment label="Why it spreads easily">
            <div className="grid gap-3 sm:grid-cols-3">
              <TraitCard
                word="More days"
                text="Delivery days are configuration, not code. Add Tuesday and it plans Tuesday."
              />
              <TraitCard
                word="More vans"
                text="The same coordination loop can sit above a multi-vehicle route solver."
              />
              <TraitCard
                word="Other trades"
                text="Anything where somebody must be home: groceries, medicine, repairs, installs."
              />
            </div>
          </Segment>

          <div className="rounded-[14px] border border-accent-edge bg-accent-wash px-7 py-7">
            <p className="font-display text-[26px] leading-[1.4] text-ink sm:text-[32px]">
              Every confirmed customer gets a time they accepted.
              <br />
              Every driver gets a route built from accepted promises.
              <br />
              <span className="text-accent">Unresolved cases go to the coordinator.</span>
            </p>
          </div>
        </Slide>
      </div>
    </div>
  );
}

/* -- slide frame ------------------------------------------------------------- */

function Slide({
  n,
  eyebrow,
  criterion,
  children,
  first = false,
  last = false,
}: {
  n: number;
  eyebrow: string;
  /** The judging criterion this slide is answering. */
  criterion: string;
  children: ReactNode;
  first?: boolean;
  last?: boolean;
}) {
  return (
    <section
      id={`slide-${n}`}
      aria-label={`Slide ${n + 1}: ${eyebrow}`}
      className={cx(
        "enter flex scroll-mt-[76px] flex-col gap-5",
        first ? "pb-14" : "py-14",
        !first && "border-t border-rail",
        last && "pb-4",
      )}
      style={{ animationDelay: `${Math.min(n, 4) * 55}ms` }}
    >
      <div className="flex items-center gap-3">
        <span className="font-mono text-[13px] font-medium tabular-nums text-ink-faint">
          {String(n + 1).padStart(2, "0")}
        </span>
        <span aria-hidden className="h-px w-6 bg-rail-strong" />
        <Eyebrow>{eyebrow}</Eyebrow>
        <span aria-hidden className="h-px flex-1 bg-rail" />
        <span
          title="The judging criterion this slide answers"
          className="font-mono text-[11.5px] uppercase tracking-[0.11em] text-ink-faint"
        >
          {criterion}
        </span>
      </div>
      {children}
    </section>
  );
}

function Headline({ children }: { children: ReactNode }) {
  return (
    <h2 className="max-w-[24ch] font-display text-[40px] leading-[1.1] tracking-[-0.015em] text-ink sm:text-[50px]">
      {children}
    </h2>
  );
}

function Lede({ children }: { children: ReactNode }) {
  return <p className="max-w-[64ch] text-[19px] leading-[1.6] text-ink-soft">{children}</p>;
}

function Strong({ children }: { children: ReactNode }) {
  return <strong className="font-semibold text-ink">{children}</strong>;
}

/** The AI / Code marker, inline in prose, matching the one on the architecture diagram. */
function Tag({ who }: { who: "ai" | "code" }) {
  return (
    <span
      className={cx(
        "inline-block rounded-full border px-2 py-[1px] align-[1px]",
        "font-mono text-[11.5px] font-semibold uppercase tracking-[0.1em]",
        who === "ai"
          ? "border-accent-edge bg-accent-wash text-accent"
          : "border-rail-strong bg-sunk text-ink-muted",
      )}
    >
      {who === "ai" ? "AI" : "Code"}
    </span>
  );
}

/** A labelled block inside a slide. This keeps each slide readable. */
function Segment({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-3.5 pt-2">
      <div className="flex items-center gap-3">
        <Eyebrow>{label}</Eyebrow>
        <span aria-hidden className="h-px flex-1 bg-rail" />
      </div>
      {children}
    </div>
  );
}

function Statement({ children }: { children: ReactNode }) {
  return (
    <div className="mt-2 rounded-[14px] border border-rail bg-surface px-6 py-5 shadow-[var(--shadow-raise)]">
      <Eyebrow>The problem, in one sentence</Eyebrow>
      <p className="mt-2 font-display text-[24px] leading-[1.42] text-ink sm:text-[26px]">
        {children}
      </p>
    </div>
  );
}

/* -- reading rail ------------------------------------------------------------ */

function Rail({ active }: { active: number }) {
  return (
    <nav
      aria-label="Slides"
      className="sticky top-[92px] hidden h-fit w-[168px] shrink-0 flex-col gap-0.5 lg:flex"
    >
      {SLIDES.map((label, i) => (
        <a
          key={label}
          href={`#slide-${i}`}
          aria-current={i === active ? "true" : undefined}
          className={cx(
            "group flex items-center gap-2.5 rounded-[7px] px-2 py-[5px] transition-colors duration-150",
            i === active ? "text-accent" : "text-ink-faint hover:text-ink-soft",
          )}
        >
          <span
            className={cx(
              "h-px shrink-0 transition-all duration-200",
              i === active ? "w-5 bg-accent" : "w-2.5 bg-rail-strong group-hover:w-4",
            )}
          />
          <span className="truncate text-[14px] leading-[1.4]">{label}</span>
        </a>
      ))}
    </nav>
  );
}

/**
 * Which slide is being read.
 *
 * Chooses the last slide whose top has passed a line a third of the way down the viewport, rather
 * than using intersection ratios -- with sections this tall, several are visible at once and the
 * "most visible" one flickers between neighbours on slow scrolls.
 */
function useActiveSlide(count: number) {
  const [active, setActive] = useState(0);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    const read = () => {
      frame.current = null;
      const line = window.innerHeight / 3;
      let current = 0;
      for (let i = 0; i < count; i += 1) {
        const el = document.getElementById(`slide-${i}`);
        if (el && el.getBoundingClientRect().top <= line) current = i;
      }
      setActive(current);
    };

    const onScroll = () => {
      if (frame.current === null) frame.current = window.requestAnimationFrame(read);
    };

    read();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
    };
  }, [count]);

  return active;
}

/* -- cards ------------------------------------------------------------------- */

function PersonCard({
  name,
  does,
  agent,
  tone,
}: {
  name: string;
  does: string;
  agent: string;
  tone: "locked" | "pending";
}) {
  return (
    <div className="flex flex-col gap-2.5 rounded-[12px] border border-rail bg-surface px-5 py-4 shadow-[var(--shadow-raise)]">
      <div className="flex items-center gap-2">
        <span className={cx("h-1.5 w-1.5 rounded-full", tone === "locked" ? "bg-locked" : "bg-pending")} />
        <span className="text-[17px] font-semibold text-ink">{name}</span>
      </div>
      <p className="text-[16px] leading-[1.5] text-ink-muted">{does}</p>
      <p className="border-t border-rail pt-2.5 text-[16px] leading-[1.5] text-ink-soft">{agent}</p>
    </div>
  );
}

function TraitCard({ word, text }: { word: string; text: string }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-[12px] border border-rail bg-surface px-5 py-4">
      <span className="font-display text-[24px] leading-none text-accent">{word}</span>
      <p className="text-[15.5px] leading-[1.5] text-ink-muted">{text}</p>
    </div>
  );
}

function DayCard({ day, regions }: { day: string; regions: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-[12px] border border-rail bg-surface px-5 py-4 shadow-[var(--shadow-raise)]">
      <span className="font-display text-[26px] leading-none text-ink">{day}</span>
      <span className="text-[15.5px] leading-[1.5] text-ink-muted">{regions}</span>
    </div>
  );
}

function ScreenCard({ href, name, text }: { href: string; name: string; text: string }) {
  return (
    <Link
      href={href}
      className={cx(
        "group flex flex-col gap-1.5 rounded-[12px] border border-rail bg-surface px-5 py-4",
        "shadow-[var(--shadow-raise)] transition-colors duration-150 hover:border-accent-edge hover:bg-accent-wash/40",
      )}
    >
      <span className="flex items-center gap-1.5 text-[17px] font-semibold text-ink">
        {name}
        <svg
          viewBox="0 0 12 12"
          aria-hidden
          className="h-3 w-3 text-ink-faint transition-transform duration-150 group-hover:translate-x-0.5"
          fill="none"
        >
          <path d="M3.5 2.5 7.5 6l-4 3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      <span className="text-[15.5px] leading-[1.5] text-ink-muted">{text}</span>
    </Link>
  );
}

function Note({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-[12px] border border-rail bg-sunk/50 px-5 py-4">
      <span className="text-[16px] font-semibold text-ink">{title}</span>
      <p className="text-[15.5px] leading-[1.55] text-ink-muted">{children}</p>
    </div>
  );
}

function TickIcon() {
  return (
    <svg viewBox="0 0 14 14" aria-hidden className="h-3.5 w-3.5 shrink-0 text-locked" fill="none">
      <path
        d="M2.75 7.4 5.6 10.2 11.25 4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Recognisable channel cue without adding an icon dependency. Path from Simple Icons (CC0). */
function WhatsAppMark() {
  return (
    <span
      aria-label="WhatsApp"
      className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-wa-accent text-white"
    >
      <svg viewBox="0 0 24 24" aria-hidden className="h-[17px] w-[17px] fill-current">
        <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z" />
      </svg>
    </span>
  );
}
