"use client";

import type { ReactNode } from "react";
import { cx } from "@/components/ui";

/**
 * A simulated phone message thread.
 *
 * Recognisability is the point: someone watching should know instantly that this is the customer's
 * side, on their phone, without being told. So it borrows the familiar layout and colour
 * conventions -- green header, tinted outgoing bubbles with tails, timestamps and delivery ticks.
 *
 * It borrows no assets. The logo and wordmark are absent, the wallpaper is drawn below rather than
 * copied, and the screen carries a "simulated" badge so nobody mistakes it for a real integration.
 * Everything here is scoped to this component; the rest of the console keeps its own identity.
 */

export function Phone({ children, status }: { children: ReactNode; status: string }) {
  return (
    <div className="flex flex-col overflow-hidden rounded-[22px] border border-rail bg-[var(--color-wa-header)] shadow-[var(--shadow-lift)]">
      <div className="flex items-center justify-between px-4 pt-2.5 pb-1">
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-white/70">
          WhatsApp Demo
        </span>
        <span className="rounded-full bg-white/15 px-2 py-[1px] font-mono text-[9.5px] uppercase tracking-[0.1em] text-white/80">
          simulated
        </span>
      </div>

      <header className="flex items-center gap-3 bg-[var(--color-wa-header)] px-3.5 pb-2.5">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--color-wa-header-deep)] text-[14px] font-semibold text-white/90">
          MF
        </div>
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-[14.5px] font-medium leading-tight text-white">
            Majestic Fighters Delivery
          </span>
          <span className="text-[11.5px] leading-tight text-white/70">{status}</span>
        </div>
      </header>

      {children}
    </div>
  );
}

/** The chat ground. An original repeating pattern of delivery motifs, drawn rather than copied. */
export function Wallpaper({ children, innerRef }: { children: ReactNode; innerRef?: React.Ref<HTMLDivElement> }) {
  return (
    <div
      ref={innerRef}
      className="flex flex-1 flex-col gap-1.5 overflow-y-auto px-3 py-3.5"
      style={{
        backgroundColor: "var(--color-wa-paper)",
        backgroundImage: `url("data:image/svg+xml,${encodeURIComponent(WALLPAPER)}")`,
        backgroundSize: "148px 148px",
      }}
    >
      {children}
    </div>
  );
}

const WALLPAPER = `<svg xmlns="http://www.w3.org/2000/svg" width="148" height="148" viewBox="0 0 148 148">
<g fill="none" stroke="#c8bfb4" stroke-opacity="0.5" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
<rect x="16" y="20" width="20" height="15" rx="2"/><path d="M16 26h20"/>
<path d="M64 28h14v10h-14z"/><path d="M78 31h5l3 4v3h-8z"/><circle cx="68" cy="41" r="2.4"/><circle cx="82" cy="41" r="2.4"/>
<path d="M110 22c0-2 2-3 4-3h8c2 0 4 1 4 3v10h-16z"/><path d="M108 32h20v5h-20z"/>
<path d="M24 88l6-5 6 5v9h-12z"/>
<path d="M62 82h20v14h-20z"/><path d="M62 88h20"/><path d="M72 82v14"/>
<circle cx="118" cy="88" r="8"/><path d="M118 83v5l3 3"/>
<path d="M20 120h16v10h-16z"/><path d="M28 120v10"/>
<path d="M66 118l8 6 8-6"/><path d="M66 118h16v12h-16z"/>
<path d="M112 118h16v12h-16z"/><path d="M116 118v-4h8v4"/>
</g></svg>`;

// -- bubbles ------------------------------------------------------------------

export function Bubble({
  from,
  children,
  time,
  ticks,
  tone = "plain",
}: {
  from: "them" | "me";
  children: ReactNode;
  time: string;
  ticks?: "sent" | "delivered" | "read";
  tone?: "plain" | "confirmed";
}) {
  const mine = from === "me";
  return (
    <div className={cx("flex w-full", mine ? "justify-end" : "justify-start")}>
      <div
        className={cx(
          "relative max-w-[85%] rounded-[8px] px-2.5 py-1.5 shadow-[0_1px_0.5px_rgba(11,20,26,0.13)]",
          mine ? "rounded-tr-[3px]" : "rounded-tl-[3px]",
          tone === "confirmed" && "ring-1 ring-[var(--color-wa-accent)]/45",
        )}
        style={{ background: mine ? "var(--color-wa-out)" : "var(--color-wa-in)" }}
      >
        <div
          className="whitespace-pre-line text-[13.5px] leading-[1.45]"
          style={{ color: "var(--color-wa-ink)" }}
        >
          {children}
        </div>
        <div className="mt-0.5 flex items-center justify-end gap-1">
          <span className="font-mono text-[10px]" style={{ color: "var(--color-wa-meta)" }}>
            {time}
          </span>
          {mine && ticks && <Ticks state={ticks} />}
        </div>
        <span
          aria-hidden
          className={cx("absolute top-0 h-3 w-3", mine ? "-right-[7px]" : "-left-[7px]")}
          style={{
            background: mine ? "var(--color-wa-out)" : "var(--color-wa-in)",
            clipPath: mine ? "polygon(0 0, 100% 0, 0 100%)" : "polygon(0 0, 100% 0, 100% 100%)",
          }}
        />
      </div>
    </div>
  );
}

function Ticks({ state }: { state: "sent" | "delivered" | "read" }) {
  const colour = state === "read" ? "var(--color-wa-tick)" : "var(--color-wa-meta)";
  return (
    <svg viewBox="0 0 18 12" className="h-3 w-[18px]" aria-label={`Message ${state}`}>
      <path
        d="M1.5 6.5 L4.5 9.5 L10 3"
        fill="none"
        stroke={colour}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {state !== "sent" && (
        <path
          d="M7 6.5 L10 9.5 L16 3"
          fill="none"
          stroke={colour}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}

export function TypingBubble() {
  return (
    <div className="flex justify-start">
      <div
        className="flex gap-1 rounded-[8px] rounded-tl-[3px] px-3 py-2.5 shadow-[0_1px_0.5px_rgba(11,20,26,0.13)]"
        style={{ background: "var(--color-wa-in)" }}
      >
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-1.5 w-1.5 animate-bounce rounded-full"
            style={{
              background: "var(--color-wa-meta)",
              animationDelay: `${i * 130}ms`,
              animationDuration: "1.1s",
            }}
          />
        ))}
        <span className="sr-only">Typing…</span>
      </div>
    </div>
  );
}

/** The reply buttons a customer taps. Kept inside the thread so a choice reads as part of the
 *  conversation rather than as a form bolted underneath it. */
export function ChoiceBubble({
  options,
  onChoose,
  onDecline,
  disabled,
}: {
  options: Array<{ id: string; label: string; sub?: string }>;
  onChoose: (id: string) => void;
  /** Declining is a normal thing a customer does, so it belongs in the thread beside the times --
   *  not in an API a demo audience never sees. Omitted once the rounds are used up. */
  onDecline?: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex w-full flex-col items-start gap-1">
      {options.map((option) => (
        <button
          key={option.id}
          disabled={disabled}
          onClick={() => onChoose(option.id)}
          className={cx(
            "w-[85%] rounded-[8px] rounded-tl-[3px] px-3 py-2 text-left transition-colors",
            "shadow-[0_1px_0.5px_rgba(11,20,26,0.13)] disabled:opacity-55",
            "hover:brightness-[0.985]",
          )}
          style={{ background: "var(--color-wa-in)" }}
        >
          <span
            className="block text-[13.5px] font-medium leading-tight"
            style={{ color: "var(--color-wa-header)" }}
          >
            {option.label}
          </span>
          {option.sub && (
            <span className="mt-0.5 block text-[11.5px]" style={{ color: "var(--color-wa-meta)" }}>
              {option.sub}
            </span>
          )}
        </button>
      ))}
      {onDecline && (
        <button
          disabled={disabled}
          onClick={onDecline}
          className={cx(
            "w-[85%] rounded-[8px] rounded-tl-[3px] border border-dashed px-3 py-2 text-left",
            "text-[13px] transition-colors disabled:opacity-55 hover:brightness-[0.985]",
          )}
          style={{
            background: "transparent",
            borderColor: "var(--color-wa-meta)",
            color: "var(--color-wa-meta)",
          }}
        >
          None of these work
        </button>
      )}
    </div>
  );
}

/**
 * The message box. A real one.
 *
 * It used to be a caption that said "Tap a time above", which made the buttons the only way to
 * say anything -- and a booking system you can only answer by tapping is a form wearing a
 * conversation's clothes. Typing is now the primary interaction and the buttons are shortcuts.
 */
export function Composer({
  value,
  onChange,
  onSend,
  disabled,
  placeholder = "Message",
}: {
  value: string;
  onChange: (next: string) => void;
  onSend: () => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const canSend = value.trim().length > 0 && !disabled;

  return (
    <form
      className="flex items-end gap-2 px-2.5 py-2"
      style={{ backgroundColor: "var(--color-wa-paper)" }}
      onSubmit={(e) => {
        e.preventDefault();
        if (canSend) onSend();
      }}
    >
      <div className="flex flex-1 items-center gap-2 rounded-[20px] bg-white px-3 py-2">
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          placeholder={placeholder}
          aria-label="Message"
          enterKeyHint="send"
          className="w-full bg-transparent text-[13.5px] text-[color:var(--color-wa-header)] outline-none placeholder:text-[color:var(--color-wa-meta)] disabled:opacity-60"
        />
      </div>
      <button
        type="submit"
        // Disabled while a reply is in flight, which is the double-send guard the customer can
        // see. The server has its own -- an identical message is answered rather than re-run --
        // because a disabled button is a courtesy, not a guarantee.
        disabled={!canSend}
        aria-label="Send"
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition-opacity disabled:opacity-45"
        style={{ background: "var(--color-wa-accent)" }}
      >
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="white" aria-hidden>
          <path d="M2.5 21 23 12 2.5 3 2.5 10l14.5 2-14.5 2z" />
        </svg>
      </button>
    </form>
  );
}

/** Optional shortcuts under the thread. Never the only way to answer -- see Composer. */
export function QuickReplies({
  replies,
  onPick,
  disabled,
}: {
  replies: Array<{ id: string; label: string }>;
  onPick: (id: string) => void;
  disabled?: boolean;
}) {
  if (replies.length === 0) return null;
  return (
    <div
      className="flex flex-wrap gap-1.5 px-2.5 pb-1.5"
      style={{ backgroundColor: "var(--color-wa-paper)" }}
    >
      {replies.map((reply) => (
        <button
          key={reply.id}
          type="button"
          disabled={disabled}
          onClick={() => onPick(reply.id)}
          className="rounded-full border px-2.5 py-1 text-[12px] transition-colors disabled:opacity-50"
          style={{
            borderColor: "var(--color-wa-accent)",
            color: "var(--color-wa-accent)",
            background: "white",
          }}
        >
          {reply.label}
        </button>
      ))}
    </div>
  );
}

export function chatTime(offsetMinutes = 0): string {
  const d = new Date(Date.now() + offsetMinutes * 60_000);
  const h = d.getHours() % 12 || 12;
  return `${h}:${String(d.getMinutes()).padStart(2, "0")} ${d.getHours() < 12 ? "am" : "pm"}`;
}
