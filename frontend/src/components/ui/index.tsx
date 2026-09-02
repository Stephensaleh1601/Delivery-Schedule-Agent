"use client";

/**
 * The shared vocabulary of the console.
 *
 * The one idea running through these: commitment reads as weight. `Card` is raised and light --
 * something still in play. `Card settled` is recessed with a pin rail -- a promise already made.
 * `Pill` uses the same semantic set, so a status reads the same in a table as on a card.
 */

import type { CSSProperties, ReactNode } from "react";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

// -- surfaces -----------------------------------------------------------------

export function Card({
  children,
  className,
  settled = false,
  tone = "neutral",
  as: Tag = "div",
  style,
}: {
  children: ReactNode;
  className?: string;
  /** A promise already made: recessed, with a pin rail. */
  settled?: boolean;
  tone?: "neutral" | "locked" | "pending" | "alert" | "accent";
  as?: "div" | "section" | "article" | "li";
  style?: CSSProperties;
}) {
  const railTone = {
    neutral: "before:bg-rail-strong",
    locked: "before:bg-locked",
    pending: "before:bg-pending",
    alert: "before:bg-alert",
    accent: "before:bg-accent",
  }[tone];

  return (
    <Tag
      className={cx(
        "rounded-[14px] border",
        settled
          ? cx(
              "relative overflow-hidden border-rail bg-sunk/70 pl-[15px]",
              "before:absolute before:inset-y-0 before:left-0 before:w-[3px] before:content-['']",
              railTone,
            )
          : "border-rail bg-surface shadow-[var(--shadow-raise)]",
        className,
      )}
      style={style}
    >
      {children}
    </Tag>
  );
}

export function SectionHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-6">
      <div className="flex flex-col gap-1">
        {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
        <h2 className="text-[19px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
        {description && <p className="max-w-[62ch] text-[13.5px] text-ink-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <span className="font-mono text-[10px] font-medium uppercase tracking-[0.13em] text-ink-faint">
      {children}
    </span>
  );
}

// -- status -------------------------------------------------------------------

type Tone = "neutral" | "locked" | "pending" | "alert" | "accent";

const PILL_TONES: Record<Tone, string> = {
  neutral: "bg-sunk text-ink-muted border-rail",
  locked: "bg-locked-wash text-locked border-locked-edge",
  pending: "bg-pending-wash text-pending border-pending-edge",
  alert: "bg-alert-wash text-alert border-alert-edge",
  accent: "bg-accent-wash text-accent border-accent-edge",
};

export function Pill({
  children,
  tone = "neutral",
  icon,
}: {
  children: ReactNode;
  tone?: Tone;
  icon?: ReactNode;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px]",
        "text-[11.5px] font-medium leading-[1.35] whitespace-nowrap",
        PILL_TONES[tone],
      )}
    >
      {icon}
      {children}
    </span>
  );
}

/** Maps a planning status to the tone that reflects how committed it is. */
export function statusTone(status: string): Tone {
  if (["confirmed", "sequenced", "dispatched", "completed"].includes(status)) return "locked";
  if (status === "offered") return "accent";
  if (["exception", "cancelled"].includes(status)) return "alert";
  return "pending";
}

export function LockIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 12 12" aria-hidden className={cx("h-3 w-3", className)} fill="none">
      <rect x="2.5" y="5.25" width="7" height="5.25" rx="1.4" fill="currentColor" />
      <path
        d="M4.25 5.25V3.9a1.75 1.75 0 0 1 3.5 0v1.35"
        stroke="currentColor"
        strokeWidth="1.15"
        strokeLinecap="round"
      />
    </svg>
  );
}

// -- figures ------------------------------------------------------------------

export function Stat({
  label,
  value,
  note,
  tone = "neutral",
  emphasis = false,
}: {
  label: string;
  value: ReactNode;
  note?: string;
  tone?: Tone;
  emphasis?: boolean;
}) {
  const valueTone = {
    neutral: "text-ink",
    locked: "text-locked",
    pending: "text-pending",
    alert: "text-alert",
    accent: "text-accent",
  }[tone];

  return (
    <div
      className={cx(
        "flex flex-col gap-0.5 rounded-[14px] border px-4 py-3.5",
        emphasis
          ? cx(
              "border-locked-edge bg-locked-wash",
              tone === "alert" && "border-alert-edge bg-alert-wash",
            )
          : "border-rail bg-surface shadow-[var(--shadow-raise)]",
      )}
    >
      <span className="font-mono text-[10px] font-medium uppercase tracking-[0.11em] text-ink-faint">
        {label}
      </span>
      <span className={cx("font-display text-[30px] leading-[1.1] tnum", valueTone)}>{value}</span>
      {note && <span className="mt-0.5 text-[12px] leading-[1.4] text-ink-muted">{note}</span>}
    </div>
  );
}

// -- controls -----------------------------------------------------------------

export function Button({
  children,
  onClick,
  variant = "secondary",
  disabled,
  busy,
  type = "button",
  className,
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  disabled?: boolean;
  busy?: boolean;
  type?: "button" | "submit";
  className?: string;
  title?: string;
}) {
  const variants = {
    primary: "bg-accent text-white border-accent hover:bg-accent-hover",
    secondary: "bg-surface text-ink border-rail-strong hover:bg-sunk",
    ghost: "bg-transparent text-ink-soft border-transparent hover:bg-sunk",
    danger: "bg-surface text-alert border-alert-edge hover:bg-alert-wash",
  }[variant];

  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled || busy}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-[9px] border px-3.5 py-2",
        "text-[13.5px] font-medium transition-colors duration-150",
        "disabled:cursor-not-allowed disabled:opacity-45",
        variants,
        className,
      )}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
}

function Spinner() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="h-3.5 w-3.5 animate-spin">
      <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" fill="none" />
      <path d="M14.25 8A6.25 6.25 0 0 0 8 1.75" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none" />
    </svg>
  );
}

// -- states -------------------------------------------------------------------

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("animate-pulse rounded-[8px] bg-sunk", className)} />;
}

export function LoadingPanel({ rows = 3, label }: { rows?: number; label?: string }) {
  return (
    <div className="flex flex-col gap-3" role="status" aria-busy="true" aria-label={label ?? "Loading"}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-[14px] border border-rail bg-surface p-4">
          <Skeleton className="h-3 w-[22%]" />
          <Skeleton className="h-5 w-[58%]" />
          <Skeleton className="h-3 w-[38%]" />
        </div>
      ))}
      <span className="sr-only">Loading…</span>
    </div>
  );
}

export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  const unreachable = message.includes("Can't reach");
  return (
    <div className="flex flex-col items-start gap-3 rounded-[14px] border border-alert-edge bg-alert-wash px-5 py-4">
      <div className="flex flex-col gap-1">
        <span className="text-[14px] font-semibold text-alert">
          {unreachable ? "The dispatch service isn't responding" : "That didn't work"}
        </span>
        <p className="max-w-[62ch] text-[13px] text-ink-soft">{message}</p>
        {unreachable && (
          <p className="mt-1 font-mono text-[11.5px] text-ink-muted">
            uvicorn dispatch_agent.webapp.main:app --reload
          </p>
        )}
      </div>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function EmptyPanel({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2.5 rounded-[14px] border border-dashed border-rail-strong bg-surface/60 px-6 py-11 text-center">
      <span className="text-[14px] font-semibold text-ink-soft">{title}</span>
      {description && <p className="max-w-[46ch] text-[13px] text-ink-muted">{description}</p>}
      {action}
    </div>
  );
}

// -- table --------------------------------------------------------------------

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx("overflow-x-auto rounded-[14px] border border-rail bg-surface", className)}>
      <table className="w-full border-collapse text-[13.5px]">{children}</table>
    </div>
  );
}

export function Th({
  children,
  align = "left",
  className,
}: {
  children?: ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <th
      scope="col"
      className={cx(
        "border-b border-rail bg-sunk/60 px-4 py-2.5",
        "font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-ink-faint",
        align === "right" ? "text-right" : "text-left",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  className,
  colSpan,
}: {
  children?: ReactNode;
  align?: "left" | "right";
  className?: string;
  colSpan?: number;
}) {
  return (
    <td
      colSpan={colSpan}
      className={cx(
        "border-b border-rail/70 px-4 py-3 align-middle",
        align === "right" ? "text-right" : "text-left",
        className,
      )}
    >
      {children}
    </td>
  );
}
