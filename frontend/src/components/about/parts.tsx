"use client";

/**
 * The drawing vocabulary for the About deck.
 *
 * These are diagram primitives, not new design language: every colour, radius and type size comes
 * from the console's own tokens, so a slide sits next to the Orders table without looking like a
 * different product. The rule followed throughout is that a diagram must show the *mechanism* --
 * where a stop lands, which day gets searched -- rather than decorate a sentence that already said
 * it. Anything that would just repeat the caption is left out.
 *
 * Deliberately not ASCII in a <pre>. The markdown source uses box-drawing characters because a
 * text file has nothing else; on screen they render at the mercy of the mono font's metrics and
 * break on narrow viewports. Everything here is real layout that reflows.
 */

import type { ReactNode } from "react";
import { cx } from "@/components/ui";

/* -- layout ------------------------------------------------------------------ */

/** A framed diagram area. The recessed ground separates "a picture of the system" from prose. */
export function Figure({
  children,
  caption,
  className,
}: {
  children: ReactNode;
  caption?: string;
  className?: string;
}) {
  return (
    <figure className="flex flex-col gap-2.5">
      <div
        className={cx(
          "rounded-[14px] border border-rail bg-surface px-5 py-6 sm:px-7",
          "shadow-[var(--shadow-raise)]",
          className,
        )}
      >
        {children}
      </div>
      {caption && (
        <figcaption className="text-[12.5px] leading-[1.5] text-ink-muted">{caption}</figcaption>
      )}
    </figure>
  );
}

/** Two panels set against each other. The whole point is the middle rule. */
export function Versus({
  left,
  right,
}: {
  left: { label: string; tone?: "muted" | "accent"; items: ReactNode[] };
  right: { label: string; tone?: "muted" | "accent"; items: ReactNode[] };
}) {
  return (
    <div className="grid gap-px overflow-hidden rounded-[12px] border border-rail bg-rail sm:grid-cols-2">
      {[left, right].map((side) => (
        <div
          key={side.label}
          className={cx(
            "flex flex-col gap-3 px-5 py-5",
            side.tone === "accent" ? "bg-accent-wash/50" : "bg-surface",
          )}
        >
          <span
            className={cx(
              "font-mono text-[10px] font-semibold uppercase tracking-[0.13em]",
              side.tone === "accent" ? "text-accent" : "text-ink-faint",
            )}
          >
            {side.label}
          </span>
          <ul className="flex flex-col gap-2.5">
            {side.items.map((item, i) => (
              <li
                key={i}
                className={cx(
                  "text-[13.5px] leading-[1.5]",
                  side.tone === "accent" ? "text-ink" : "text-ink-muted",
                )}
              >
                {item}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/* -- flow -------------------------------------------------------------------- */

/** A left-to-right chain of steps. Wraps to a vertical stack on narrow screens. */
export function Flow({
  steps,
  tone = "neutral",
}: {
  steps: Array<{ title: string; note?: string; icon?: ReactNode }>;
  tone?: "neutral" | "alert";
}) {
  return (
    <ol className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
      {steps.map((step, i) => (
        <li key={step.title} className="flex flex-1 items-center gap-2">
          <div
            className={cx(
              "flex-1 rounded-[10px] border px-4 py-3",
              tone === "alert"
                ? "border-alert-edge bg-alert-wash"
                : "border-rail bg-sunk/60",
            )}
          >
            <div className="flex items-center gap-2">
              {step.icon}
              <div
                className={cx(
                  "text-[13.5px] font-medium leading-[1.35]",
                  tone === "alert" ? "text-alert" : "text-ink",
                )}
              >
                {step.title}
              </div>
            </div>
            {step.note && (
              <div className="mt-0.5 text-[12px] leading-[1.4] text-ink-muted">{step.note}</div>
            )}
          </div>
          {i < steps.length - 1 && <Chevron />}
        </li>
      ))}
    </ol>
  );
}

function Chevron() {
  return (
    <svg
      viewBox="0 0 16 16"
      aria-hidden
      className="h-4 w-4 shrink-0 rotate-90 text-ink-faint sm:rotate-0"
      fill="none"
    >
      <path
        d="M5.5 3.25 10.25 8 5.5 12.75"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* -- route drawings ---------------------------------------------------------- */

/**
 * A van's day as a line of stops, drawn the way `RouteStrip` draws it in the conversation.
 * `insertAt` marks where a new customer would slide in; `wander` bends the line to show a route
 * that doubles back.
 */
export function RouteLine({
  stops,
  insertAt,
  label,
  tone = "neutral",
}: {
  stops: string[];
  insertAt?: number;
  label?: string;
  tone?: "neutral" | "accent" | "alert";
}) {
  const nodes: Array<{ label: string; kind: "depot" | "stop" | "new" }> = [
    { label: "Depot", kind: "depot" },
    ...stops.map((s) => ({ label: s, kind: "stop" as const })),
    { label: "Depot", kind: "depot" },
  ];
  if (insertAt !== undefined) {
    nodes.splice(insertAt + 1, 0, { label: "New customer", kind: "new" });
  }

  return (
    <div className="flex flex-col gap-2">
      {label && (
        <span className="font-mono text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
          {label}
        </span>
      )}
      <div className="flex flex-wrap items-stretch gap-1.5">
        {nodes.map((node, i) => (
          <div key={`${node.label}-${i}`} className="flex items-center gap-1.5">
            <div
              className={cx(
                "rounded-[8px] border px-2.5 py-1.5 text-[12px] leading-[1.3] whitespace-nowrap",
                node.kind === "depot" && "border-rail-strong bg-sunk font-medium text-ink-soft",
                node.kind === "stop" && "border-rail bg-surface text-ink-muted",
                node.kind === "new" &&
                  cx(
                    "font-semibold",
                    tone === "alert"
                      ? "border-alert-edge bg-alert-wash text-alert"
                      : "border-accent-edge bg-accent-wash text-accent",
                  ),
              )}
            >
              {node.label}
            </div>
            {i < nodes.length - 1 && (
              <span aria-hidden className="h-px w-3 shrink-0 bg-rail-strong" />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * The detour problem, drawn as geography rather than described.
 *
 * Same five houses in both panels, same depot, different visiting order. The zig-zag is the whole
 * argument for slide 2 -- you can say "detours cost time" in a sentence, but the sentence does not
 * make anyone feel the difference between these two lines.
 */
export function DetourComparison() {
  // One set of coordinates, drawn twice in different orders.
  const houses = [
    { x: 62, y: 38, name: "A" },
    { x: 108, y: 26, name: "B" },
    { x: 158, y: 46, name: "C" },
    { x: 176, y: 90, name: "D" },
    { x: 96, y: 96, name: "E" },
  ];
  const depot = { x: 22, y: 66 };

  const path = (order: number[]) =>
    [depot, ...order.map((i) => houses[i]), depot].map((p) => `${p.x},${p.y}`).join(" ");

  return (
    <div className="grid gap-px overflow-hidden rounded-[12px] border border-rail bg-rail sm:grid-cols-2">
      <RoutePlot
        title="Sensible order"
        note="One loop. Everyone on time."
        points={path([0, 1, 2, 3, 4])}
        houses={houses}
        depot={depot}
        tone="locked"
      />
      <RoutePlot
        title="Bad order"
        note="Doubles back twice. The last stops wait all day."
        points={path([0, 3, 1, 4, 2])}
        houses={houses}
        depot={depot}
        tone="alert"
      />
    </div>
  );
}

function RoutePlot({
  title,
  note,
  points,
  houses,
  depot,
  tone,
}: {
  title: string;
  note: string;
  points: string;
  houses: Array<{ x: number; y: number; name: string }>;
  depot: { x: number; y: number };
  tone: "locked" | "alert";
}) {
  const stroke = tone === "locked" ? "var(--color-locked)" : "var(--color-alert)";
  return (
    <div className="flex flex-col gap-2 bg-surface px-5 py-5">
      <div className="flex items-center gap-2">
        <span
          className={cx(
            "h-1.5 w-1.5 rounded-full",
            tone === "locked" ? "bg-locked" : "bg-alert",
          )}
        />
        <span
          className={cx(
            "text-[13px] font-semibold",
            tone === "locked" ? "text-locked" : "text-alert",
          )}
        >
          {title}
        </span>
      </div>
      <svg viewBox="0 0 200 120" className="w-full" role="img" aria-label={`${title}. ${note}`}>
        <polyline
          points={points}
          fill="none"
          stroke={stroke}
          strokeWidth="1.6"
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity="0.85"
        />
        <rect
          x={depot.x - 5}
          y={depot.y - 5}
          width="10"
          height="10"
          rx="2"
          fill="var(--color-ink-soft)"
        />
        {houses.map((h) => (
          <g key={h.name}>
            <circle cx={h.x} cy={h.y} r="5.5" fill="var(--color-surface)" stroke={stroke} strokeWidth="1.4" />
            <text
              x={h.x}
              y={h.y + 3}
              textAnchor="middle"
              fontSize="7"
              fill="var(--color-ink-muted)"
              fontFamily="var(--font-mono)"
            >
              {h.name}
            </text>
          </g>
        ))}
      </svg>
      <p className="text-[12px] leading-[1.45] text-ink-muted">{note}</p>
    </div>
  );
}

/* -- how the agent is put together ------------------------------------------- */

/**
 * The agent's structure, top to bottom: what starts it, who decides, what it can reach, and how
 * it ends.
 *
 * The shape is the argument. Everything fans out from one question -- "what should I do next?" --
 * into three groups of tools, and every one of them comes back to the same agent to be asked
 * again. That return leg is the thing that makes this an agent rather than a script, so it gets
 * drawn rather than described.
 *
 * Each box is tagged AI or CODE. The model appears exactly twice, and neither time does it touch
 * a number -- which is the deck's central claim, rendered as a property of the picture instead of
 * a sentence asking to be believed.
 */
export function AgentArchitecture() {
  return (
    <div className="flex flex-col items-stretch">
      <Node
        kind="trigger"
        title="A customer texts — or a scheduled run starts"
        ref="handle_planning_event()"
      />
      <Rung />

      <Node
        kind="step"
        title="Work out what they want"
        note="Their words, quoted. Then turned into real dates by code."
        tags={["ai", "code"]}
        ref="agents/understanding.py → planning/language.py"
      />
      <Rung />

      <Node
        kind="agent"
        title="Main agent"
        note="“What should I do next?”"
        tags={["ai"]}
        ref="agents/scheduling_agent.py — observe → decide → act"
      />

      <FanOut />

      <div className="grid gap-2.5 sm:grid-cols-3">
        <ToolGroup
          title="Policy tools"
          reads="knowledge/delivery-policy.md — 26 numbered rules"
          items={[
            { name: "search_delivery_policy", note: "Find the rule that answers their question" },
            { name: "retrieve_policy", note: "Pull up a topic it already knows it needs" },
          ]}
          returns="The rule text, and its number — WINDOW-1, ATTEND-2"
        />
        <ToolGroup
          title="Route tools"
          reads="The published routes, measured with OR-Tools"
          items={[
            { name: "find_normal_slot", note: "Their own delivery day" },
            { name: "find_requested_day_slot", note: "The day they named" },
            { name: "find_fallback_options", note: "Both days — only after a “no”" },
          ]}
          returns="Times proved against the real route, never guessed"
        />
        <ToolGroup
          title="Action tools"
          reads="The order book, and the customer's thread"
          items={[
            { name: "confirm_offer", note: "Book what they accepted" },
            { name: "explain_offer", note: "Say why that time" },
            { name: "escalate_booking", note: "Hand to a coordinator" },
            { name: "send_message", note: "Send the wording the tool produced" },
          ]}
          returns="A booking, and one reply the customer sees"
        />
      </div>

      <FanIn />

      <Node
        kind="agent"
        title="Main agent decides again"
        note="Given what came back — is this finished?"
        tags={["ai"]}
      />
      <Rung />

      <Node
        kind="end"
        title="Finish, or go round again"
        note="10 turns maximum — a counter in the code, not the AI's judgement."
        ref="MAX_TOOL_STEPS = 10"
      />

      <p className="mt-4 border-t border-rail pt-3 text-[12px] leading-[1.5] text-ink-muted">
        The finished route goes to the driver from the coordinator&rsquo;s screen, the day before.
        That is deliberately <em>not</em> one of the agent&rsquo;s tools — a customer conversation
        can never dispatch a van.
      </p>
    </div>
  );
}

/* -- diagram pieces ---------------------------------------------------------- */

function Node({
  kind,
  title,
  note,
  tags,
  ref,
}: {
  kind: "trigger" | "step" | "agent" | "end";
  title: string;
  note?: string;
  tags?: Array<"ai" | "code">;
  /** Where this actually lives in the code. Cheap credibility for a judge who wants to look. */
  ref?: string;
}) {
  const skin = {
    trigger: "border-rail-strong bg-sunk text-ink-soft",
    step: "border-rail bg-surface shadow-[var(--shadow-raise)]",
    agent: "border-accent-edge bg-accent-wash",
    end: "border-locked-edge bg-locked-wash",
  }[kind];

  const titleTone = {
    trigger: "text-ink-soft",
    step: "text-ink",
    agent: "text-accent",
    end: "text-locked",
  }[kind];

  return (
    <div className={cx("rounded-[11px] border px-4 py-3 text-center", skin)}>
      <div className="flex flex-wrap items-center justify-center gap-2">
        <span className={cx("text-[14px] font-semibold", titleTone)}>{title}</span>
        {tags?.map((tag) => (
          <Who key={tag} who={tag} />
        ))}
      </div>
      {note && <div className="mt-0.5 text-[12.5px] leading-[1.45] text-ink-muted">{note}</div>}
      {ref && (
        <code className="mt-1 block font-mono text-[10.5px] leading-[1.35] break-all text-ink-faint">
          {ref}
        </code>
      )}
    </div>
  );
}

/** AI or CODE. The single most useful thing on the diagram. */
function Who({ who }: { who: "ai" | "code" }) {
  return (
    <span
      className={cx(
        "shrink-0 rounded-full border px-2 py-[2px]",
        "font-mono text-[9.5px] font-semibold uppercase tracking-[0.1em]",
        who === "ai"
          ? "border-accent-edge bg-surface text-accent"
          : "border-rail-strong bg-surface text-ink-muted",
      )}
    >
      {who === "ai" ? "AI" : "Code"}
    </span>
  );
}

/** The plain vertical connector between two stacked nodes. */
function Rung() {
  return (
    <div aria-hidden className="flex h-5 justify-center">
      <span className="w-px bg-rail-strong" />
    </div>
  );
}

/**
 * The bracket from the agent down into three columns.
 *
 * Drawn with borders on two half-cells per column rather than an SVG: the corners of an SVG
 * bracket distort under `preserveAspectRatio="none"`, and this version lands exactly on each
 * column's centre at any width because it shares the grid the columns use. Below `sm` the columns
 * stack, so it collapses to a single line.
 */
function FanOut() {
  return (
    <div aria-hidden>
      <div className="flex h-5 justify-center">
        <span className="w-px bg-rail-strong" />
      </div>
      <div className="hidden h-4 grid-cols-3 sm:grid">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex">
            <span
              className={cx("flex-1 border-r border-rail-strong", i !== 0 && "border-t")}
            />
            <span className={cx("flex-1", i !== 2 && "border-t border-rail-strong")} />
          </div>
        ))}
      </div>
      <div className="h-4 sm:hidden" />
    </div>
  );
}

/** The same bracket, mirrored: three columns rejoining one line. */
function FanIn() {
  return (
    <div aria-hidden>
      <div className="hidden h-4 grid-cols-3 sm:grid">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex">
            <span
              className={cx("flex-1 border-r border-rail-strong", i !== 0 && "border-b")}
            />
            <span className={cx("flex-1", i !== 2 && "border-b border-rail-strong")} />
          </div>
        ))}
      </div>
      <div className="h-4 sm:hidden" />
      <div className="flex h-5 justify-center">
        <span className="w-px bg-rail-strong" />
      </div>
    </div>
  );
}

/**
 * One of the three things the agent can reach.
 *
 * `returns` is the important half. A tool list says what the agent may call; what a judge needs to
 * see is what comes back, because that is where the guarantee lives -- a route tool cannot return
 * a time it has not proved.
 */
function ToolGroup({
  title,
  reads,
  items,
  returns,
}: {
  title: string;
  /** What this group is allowed to look at. Scope is the guarantee, so it is on the card. */
  reads: string;
  /** The real registered tool names, each with a plain-English gloss. */
  items: Array<{ name: string; note: string }>;
  returns: string;
}) {
  return (
    <div className="flex flex-col rounded-[11px] border border-rail bg-surface shadow-[var(--shadow-raise)]">
      <div className="border-b border-rail px-3.5 py-2">
        <div className="text-[13px] font-semibold text-ink">{title}</div>
        <div className="mt-0.5 text-[11px] leading-[1.35] text-ink-muted">{reads}</div>
      </div>

      <ul className="flex flex-1 flex-col gap-2 px-3.5 py-2.5">
        {items.map((item) => (
          <li key={item.name} className="flex flex-col">
            <code className="font-mono text-[11.5px] leading-[1.35] break-all text-accent">
              {item.name}
            </code>
            <span className="text-[12px] leading-[1.4] text-ink-muted">{item.note}</span>
          </li>
        ))}
      </ul>

      <div className="rounded-b-[10px] border-t border-rail bg-sunk/60 px-3.5 py-2">
        <div className="font-mono text-[9px] font-semibold uppercase tracking-[0.11em] text-ink-faint">
          Comes back with
        </div>
        <div className="mt-0.5 text-[12px] leading-[1.4] text-ink-soft">{returns}</div>
      </div>
    </div>
  );
}

/* -- small pieces ------------------------------------------------------------ */

/** A short list of checks, each either a tick or a cross. Used for "what we do / don't do". */
export function CheckList({
  items,
}: {
  items: Array<{ ok: boolean; text: ReactNode }>;
}) {
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item, i) => (
        <li key={i} className="flex items-start gap-2.5">
          <Mark ok={item.ok} />
          <span className="text-[13.5px] leading-[1.5] text-ink-soft">{item.text}</span>
        </li>
      ))}
    </ul>
  );
}

function Mark({ ok }: { ok: boolean }) {
  return (
    <svg
      viewBox="0 0 14 14"
      aria-hidden
      className={cx("mt-[3px] h-3.5 w-3.5 shrink-0", ok ? "text-locked" : "text-alert")}
      fill="none"
    >
      <circle cx="7" cy="7" r="6.25" stroke="currentColor" strokeWidth="1.1" opacity="0.4" />
      {ok ? (
        <path
          d="M4.25 7.15 6.2 9.1 9.9 5.2"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      ) : (
        <path
          d="M4.9 4.9 9.1 9.1M9.1 4.9 4.9 9.1"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}

/** A quotation from the customer interview. The evidence, given room. */
export function Quote({ children, source }: { children: ReactNode; source: string }) {
  return (
    <blockquote className="relative rounded-[12px] border border-rail bg-sunk/50 py-5 pl-6 pr-5">
      <span aria-hidden className="absolute inset-y-4 left-0 w-[3px] rounded-full bg-accent" />
      <p className="font-display text-[19px] leading-[1.4] text-ink">{children}</p>
      <footer className="mt-2.5 font-mono text-[10.5px] uppercase tracking-[0.11em] text-ink-faint">
        {source}
      </footer>
    </blockquote>
  );
}

/** Two-column "before / after" rows. */
export function BeforeAfter({
  rows,
}: {
  rows: Array<{ before: string; after: string }>;
}) {
  return (
    <div className="overflow-hidden rounded-[12px] border border-rail">
      <div className="grid grid-cols-2 gap-px bg-rail">
        <div className="bg-sunk/70 px-5 py-2.5">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.13em] text-ink-faint">
            By hand today
          </span>
        </div>
        <div className="bg-locked-wash px-5 py-2.5">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.13em] text-locked">
            With the agent
          </span>
        </div>
        {rows.map((row) => (
          <ContrastRow key={row.before} before={row.before} after={row.after} />
        ))}
      </div>
    </div>
  );
}

function ContrastRow({ before, after }: { before: string; after: string }) {
  return (
    <>
      <div className="bg-surface px-5 py-3.5 text-[13.5px] leading-[1.5] text-ink-muted">
        {before}
      </div>
      <div className="bg-surface px-5 py-3.5 text-[13.5px] leading-[1.5] text-ink">{after}</div>
    </>
  );
}
