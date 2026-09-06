"use client";

/**
 * The deck, as a page.
 *
 * Ten slides, read top to bottom, one idea each. The audience is someone meeting this for the
 * first time -- a judge, a new coordinator, the client -- so every slide leads with a picture and
 * says the least that makes the picture make sense.
 *
 * Each slide carries the judging criterion it answers. That is not decoration: the brief scores
 * five things at 20% each, and a deck where nobody can tell which slide argues "originality" has
 * left points on the table. The tags also keep the copy honest -- a slide that cannot name its
 * criterion is a slide that is not doing work.
 *
 * Source of truth is `slides.md` at the repo root; this renders the same ten. Move one, move both.
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
  Quote,
  RouteLine,
  Versus,
} from "@/components/about/parts";

const SLIDES = [
  "The ask",
  "The problem",
  "What we built",
  "Why an agent",
  "How it is built",
  "The one big rule",
  "Fitting people in",
  "The prototype",
  "How we know",
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
            An AI helper that agrees a delivery time with every customer by text — and only offers
            times the van can actually make.
          </Lede>

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Pill tone="accent">Floof.sg — fresh pet food, Singapore</Pill>
            <Pill>IGNITE Agentic AI Hackathon 2026</Pill>
            <Pill>Team Majestic Fighters</Pill>
          </div>

          <p className="text-[14px] leading-[1.6] text-ink-muted">
            The three tabs beside this one are the working product.{" "}
            <Link href="/chat" className="text-accent underline underline-offset-2">
              Customer Chat
            </Link>{" "}
            is where you can watch it think.
          </p>
        </Slide>

        {/* 02 ------------------------------------------------------------- */}
        <Slide n={1} eyebrow="The problem" criterion="Presentation">
          <Headline>Two hard jobs, both done by hand</Headline>
          <Lede>
            Fresh pet food spoils in Singapore&rsquo;s heat, so it can&rsquo;t be left at the door.{" "}
            <Strong>Somebody has to be home.</Strong> Every order needs a time the customer agreed
            to.
          </Lede>

          <Segment label="Job 1 — asking everybody">
            <Figure>
              <Flow
                steps={[
                  { title: "40 orders", note: "for the week" },
                  { title: "One person", note: "an afternoon on WhatsApp" },
                  { title: "One “no”", note: "and you start again" },
                ]}
              />
            </Figure>
            <Quote source="Floof.sg — interview, 5 September 2026">
              “It&rsquo;s more to negotiate with the customers when is the delivery time slots they
              prefer. That&rsquo;s the hardest part.”
            </Quote>
          </Segment>

          <Segment label="Job 2 — deciding who to visit first">
            <Figure caption="Same five customers, same van, same day. Only the order changed.">
              <DetourComparison />
            </Figure>
            <p className="text-[14px] leading-[1.6] text-ink-soft">
              Detours turn into late deliveries. A person works this out by hand — and drivers still
              change it on the road, so the plan and the day drift apart.
            </p>
          </Segment>

          <Statement>
            A coordinator at Floof.sg needs to agree a time with every customer <em>and</em> put
            them in a sensible driving order, <Strong>because</Strong> doing both by hand takes an
            afternoon — and one “sorry, not Friday” undoes it.
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
                does="Says no. Twice."
                agent="Looks wider, offers three, or hands it to a person."
              />
            </div>
            <p className="text-[13.5px] leading-[1.55] text-ink-muted">
              Roughly 7 in 10 customers are Mrs Chua. We built it so the other 3 still get an answer
              instead of an apology.
            </p>
          </Segment>
        </Slide>

        {/* 04 ------------------------------------------------------------- */}
        <Slide n={3} eyebrow="Why an agent" criterion="Originality">
          <Headline>A form can take a time. It can&rsquo;t have a conversation.</Headline>

          <Figure>
            <Versus
              left={{
                label: "A booking form",
                items: [
                  "Fixed question, fixed answer",
                  "Offers whatever slot is free",
                  "Gives up at “no”",
                  "Answers nothing else",
                ],
              }}
              right={{
                label: "Our agent",
                tone: "accent",
                items: [
                  <>
                    Reads what they wrote — <em>“Sat morning”</em>, <em>“after 1”</em>,{" "}
                    <em>“not Friday”</em>
                  </>,
                  "Offers only what the van can do, and says why",
                  "Searches somewhere else at “no”",
                  <>
                    Answers <em>“why that time?”</em> and <em>“can you leave it at my door?”</em>
                  </>,
                ],
              }}
            />
          </Figure>

          <Segment label="“Agent” means software that does three things">
            <div className="grid gap-3 sm:grid-cols-3">
              <TraitCard word="Plans" text="Works out where this customer fits into a busy day." />
              <TraitCard word="Acts" text="Offers, books, replies, updates the route." />
              <TraitCard word="Adapts" text="A “no” changes where it searches, not just what it says." />
            </div>
          </Segment>
        </Slide>

        {/* 05 ------------------------------------------------------------- */}
        <Slide n={4} eyebrow="How it is built" criterion="Technical quality">
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

        {/* 06 ------------------------------------------------------------- */}
        <Slide n={5} eyebrow="The one big rule" criterion="Originality">
          <Headline>The AI chooses. The maths decides what is true.</Headline>

          <Figure>
            <Versus
              left={{
                label: "The AI",
                tone: "accent",
                items: [
                  "Reads the message",
                  "Picks one of six actions",
                  <>
                    Repeats their words: <em>“Saturday morning”</em>
                  </>,
                  "Never works out a date. Never invents a time.",
                ],
              }}
              right={{
                label: "The code",
                items: [
                  "Turns those words into real dates",
                  "Measures the real driving",
                  "Proves the time fits",
                  "Ranks the options. Never guesses.",
                ],
              }}
            />
          </Figure>

          <p className="text-[14px] leading-[1.6] text-ink-soft">
            AI sounds confident when it is wrong. <em>“Next Tuesday is the 15th”</em> — is it? So it
            never does the sums. <Strong>There is nowhere for a made-up date to get in.</Strong>
          </p>

          <Segment label="We tested this on a real AI. Twice it misbehaved.">
            <div className="grid gap-3 sm:grid-cols-2">
              <MisbehaviourCard
                did="Booked a slot while its own question was still unanswered."
                fix="The code now refuses unless the customer really said yes."
              />
              <MisbehaviourCard
                did="Rewrote our message as “Dear Mrs Lee… Best regards” and deleted the reason."
                fix="The code now sends the message, not the AI's rewrite."
              />
            </div>
            <p className="text-[13.5px] leading-[1.55] text-ink-muted">
              Asking it nicely did not work. So we made both impossible.
            </p>
          </Segment>
        </Slide>

        {/* 07 ------------------------------------------------------------- */}
        <Slide n={6} eyebrow="Fitting people in" criterion="Technical quality">
          <Headline>Slide them into the day. Don&rsquo;t rebuild it.</Headline>
          <Lede>
            Two delivery days a week, each covering one part of Singapore. An address already tells
            us the normal day.
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

        {/* 08 ------------------------------------------------------------- */}
        <Slide n={7} eyebrow="The prototype" criterion="Technical quality">
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
                  ["Read the message", "0.9s"],
                  ["Checked Friday's route — 17 stops", "2.3s"],
                  ["Offered 2–5pm, right after Chen Li Hua", "0.4s"],
                ].map(([text, time]) => (
                  <li
                    key={text}
                    className="flex items-center justify-between gap-4 rounded-[9px] border border-rail bg-sunk/50 px-3.5 py-2.5"
                  >
                    <span className="flex items-center gap-2.5 text-[13.5px] text-ink">
                      <TickIcon />
                      {text}
                    </span>
                    <span className="font-mono text-[11.5px] tnum text-ink-faint">{time}</span>
                  </li>
                ))}
              </ol>
            </Figure>
          </Segment>

          <Segment label="What production still needs">
            <div className="grid gap-3 sm:grid-cols-2">
              <Note title="Ready now">
                The agent, the route maths, the rules, the console, the database. Python, FastAPI,
                Next.js, OR-Tools, Claude on AWS Bedrock.
              </Note>
              <Note title="Swap-ins, clearly marked">
                Real WhatsApp instead of our simulated thread, and Floof.sg&rsquo;s own address
                lookup. Both sit behind one interface each, with a working stand-in today.
              </Note>
            </div>
          </Segment>
        </Slide>

        {/* 09 ------------------------------------------------------------- */}
        <Slide n={8} eyebrow="How we know" criterion="Effectiveness">
          <Headline>We check it. We don&rsquo;t just claim it.</Headline>

          <div className="grid gap-3 sm:grid-cols-2">
            <Metric figure="440" label="Automatic tests" note="Every rule on the last slide is one of them." />
            <Metric figure="0" label="Tests that call the internet" note="On purpose — results never drift." />
            <Metric figure="10" label="Steps, then it stops" note="A counter it cannot argue with." />
            <Metric figure="100%" label="Replies traceable to a rule" note="Every answer names the rule behind it." />
          </div>

          <Segment label="Two things we chose not to do">
            <div className="grid gap-3 sm:grid-cols-2">
              <Note title="No single “score”.">
                One number mixing driving minutes with invented penalties looks meaningful and
                isn&rsquo;t. We show the parts: minutes, kilometres, extra hours.
              </Note>
              <Note title="The AI never describes the route.">
                It would say “we&rsquo;ll be in the East that morning” when the van isn&rsquo;t.
                Those sentences are built from the real planned route.
              </Note>
            </div>
          </Segment>
        </Slide>

        {/* 10 ------------------------------------------------------------- */}
        <Slide n={9} eyebrow="What changes" criterion="Benefits" last>
          <Headline>An afternoon of texting becomes something that answers itself</Headline>

          <BeforeAfter
            rows={[
              { before: "One person, one afternoon, forty customers", after: "Each customer answered as they reply" },
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
                text="The route maths already solves a day. A second van is a second day to solve."
              />
              <TraitCard
                word="Other trades"
                text="Anything where somebody must be home: groceries, medicine, repairs, installs."
              />
            </div>
          </Segment>

          <div className="rounded-[14px] border border-accent-edge bg-accent-wash px-7 py-7">
            <p className="font-display text-[22px] leading-[1.4] text-ink sm:text-[26px]">
              Every customer gets a time they agreed to.
              <br />
              Every driver gets a route that makes sense.
              <br />
              <span className="text-accent">Nobody spends an afternoon on WhatsApp.</span>
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
        <span className="font-mono text-[11px] font-medium tabular-nums text-ink-faint">
          {String(n + 1).padStart(2, "0")}
        </span>
        <span aria-hidden className="h-px w-6 bg-rail-strong" />
        <Eyebrow>{eyebrow}</Eyebrow>
        <span aria-hidden className="h-px flex-1 bg-rail" />
        <span
          title="The judging criterion this slide answers"
          className="font-mono text-[9.5px] uppercase tracking-[0.11em] text-ink-faint"
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
    <h2 className="max-w-[24ch] font-display text-[32px] leading-[1.1] tracking-[-0.015em] text-ink sm:text-[40px]">
      {children}
    </h2>
  );
}

function Lede({ children }: { children: ReactNode }) {
  return <p className="max-w-[64ch] text-[15.5px] leading-[1.6] text-ink-soft">{children}</p>;
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
        "font-mono text-[9.5px] font-semibold uppercase tracking-[0.1em]",
        who === "ai"
          ? "border-accent-edge bg-accent-wash text-accent"
          : "border-rail-strong bg-sunk text-ink-muted",
      )}
    >
      {who === "ai" ? "AI" : "Code"}
    </span>
  );
}

/** A labelled block inside a slide. This is what keeps ten dense slides readable. */
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
      <p className="mt-2 font-display text-[20px] leading-[1.42] text-ink sm:text-[22px]">
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
          <span className="truncate text-[12px] leading-[1.4]">{label}</span>
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
        <span className="text-[14px] font-semibold text-ink">{name}</span>
      </div>
      <p className="text-[13.5px] leading-[1.5] text-ink-muted">{does}</p>
      <p className="border-t border-rail pt-2.5 text-[13.5px] leading-[1.5] text-ink-soft">{agent}</p>
    </div>
  );
}

function TraitCard({ word, text }: { word: string; text: string }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-[12px] border border-rail bg-surface px-5 py-4">
      <span className="font-display text-[20px] leading-none text-accent">{word}</span>
      <p className="text-[13px] leading-[1.5] text-ink-muted">{text}</p>
    </div>
  );
}

function MisbehaviourCard({ did, fix }: { did: string; fix: string }) {
  return (
    <div className="flex flex-col gap-2.5 rounded-[12px] border border-alert-edge bg-alert-wash/60 px-5 py-4">
      <p className="text-[13.5px] leading-[1.5] text-alert">{did}</p>
      <p className="border-t border-alert-edge/70 pt-2.5 text-[13.5px] leading-[1.5] text-ink-soft">
        {fix}
      </p>
    </div>
  );
}

function DayCard({ day, regions }: { day: string; regions: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-[12px] border border-rail bg-surface px-5 py-4 shadow-[var(--shadow-raise)]">
      <span className="font-display text-[22px] leading-none text-ink">{day}</span>
      <span className="text-[13px] leading-[1.5] text-ink-muted">{regions}</span>
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
      <span className="flex items-center gap-1.5 text-[14px] font-semibold text-ink">
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
      <span className="text-[13px] leading-[1.5] text-ink-muted">{text}</span>
    </Link>
  );
}

function Metric({ figure, label, note }: { figure: string; label: string; note: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-[12px] border border-rail bg-surface px-5 py-4 shadow-[var(--shadow-raise)]">
      <span className="font-display text-[34px] leading-[1.05] tnum text-ink">{figure}</span>
      <span className="text-[13.5px] font-medium text-ink-soft">{label}</span>
      <span className="text-[12.5px] leading-[1.45] text-ink-muted">{note}</span>
    </div>
  );
}

function Note({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-[12px] border border-rail bg-sunk/50 px-5 py-4">
      <span className="text-[13.5px] font-semibold text-ink">{title}</span>
      <p className="text-[13px] leading-[1.55] text-ink-muted">{children}</p>
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
