"use client";

import { Card, Eyebrow, Pill, cx } from "@/components/ui";
import type { Decision, DecisionOption } from "@/lib/api";

/**
 * The business decision, in the terms a coordinator would use.
 *
 * This panel used to list the tool calls, which the per-message inspector already shows in full.
 * Repeating them told a judge nothing they could not get by opening the modal, and left the actual
 * question -- *why that time and not the other one* -- answered nowhere on the screen.
 *
 * So: what was asked for, what constrained the answer, what the options really cost, what was
 * decided, and what happened. Every figure comes from a persisted tool result. There is no
 * chain-of-thought here and there cannot be, because the inputs are typed results rather than
 * model prose.
 */
export function AgentDecision({ decision }: { decision: Decision }) {
  return (
    <div className="flex flex-col gap-4">
      {decision.asked_for && (
        <Section title="What the customer asked for">
          <p className="text-[13.5px] leading-[1.5] text-ink">{decision.asked_for}</p>
        </Section>
      )}

      {decision.constraints.length > 0 && (
        <Section title="Constraints applied">
          <ul className="flex flex-col gap-1">
            {decision.constraints.map((line) => (
              <li key={line} className="flex gap-2 text-[12.5px] leading-[1.5] text-ink-soft">
                <span aria-hidden className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-rail-strong" />
                {line}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {decision.options.length > 0 && (
        <Section title="Options compared">
          <div className="flex flex-col gap-2">
            {decision.options.map((option, i) => (
              <OptionCard key={`${option.label}-${i}`} option={option} />
            ))}
          </div>
        </Section>
      )}

      {decision.decision && (
        <Section title="Decision">
          <p className="text-[13.5px] leading-[1.5] text-ink">{decision.decision}</p>
        </Section>
      )}

      {decision.outcome.length > 0 && (
        <Section title="Outcome">
          <ul className="flex flex-col gap-1">
            {decision.outcome.map((line) => (
              <li key={line} className="flex gap-2 text-[12.5px] leading-[1.5] text-ink-soft">
                <span aria-hidden className="mt-[6px] text-[10px] text-locked">✓</span>
                {line}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <Eyebrow>{title}</Eyebrow>
      {children}
    </section>
  );
}

function OptionCard({ option }: { option: DecisionOption }) {
  if (!option.feasible) {
    return (
      <Card tone="neutral" className="flex flex-col gap-1 px-3.5 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[13px] font-medium text-ink-soft">{option.label}</span>
          <Pill tone="alert">Can&apos;t be served</Pill>
        </div>
        {option.reason && (
          <p className="text-[12px] leading-[1.45] text-ink-muted">{option.reason}</p>
        )}
      </Card>
    );
  }

  // Consequences worth flagging on their own, because each one is invisible in the driving figure
  // that used to be the whole story.
  const heavy =
    (option.day_extends_minutes ?? 0) >= 60 ||
    (option.idle_minutes ?? 0) >= 60 ||
    (option.overtime_minutes ?? 0) > 0;

  return (
    <Card
      tone={option.chosen ? "locked" : "neutral"}
      className="flex flex-col gap-2 px-3.5 py-2.5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[13px] font-medium text-ink">{option.label}</span>
        <div className="flex items-center gap-1.5">
          {option.chosen && <Pill tone="locked">Offered</Pill>}
          {option.origin === "suggested" && <Pill tone="pending">We&apos;d have to ask</Pill>}
          {!option.chosen && option.origin === "requested" && <Pill tone="neutral">Requested</Pill>}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5">
        <Figure label="Driving" value={signedMinutes(option.added_drive_minutes)} />
        <Figure label="Distance" value={signedKm(option.added_distance_km)} />
        <Figure
          label="Day ends"
          value={
            option.finishes_before && option.finishes_after
              ? `${option.finishes_before} → ${option.finishes_after}`
              : (option.finishes_after ?? "—")
          }
        />
        <Figure
          label="Working day"
          value={signedMinutes(option.day_extends_minutes)}
          warn={(option.day_extends_minutes ?? 0) >= 60}
        />
        <Figure
          label="Crew waiting"
          value={signedMinutes(option.idle_minutes)}
          warn={(option.idle_minutes ?? 0) >= 60}
        />
        <Figure
          label="Overtime"
          value={option.overtime_minutes ? duration(option.overtime_minutes) : "—"}
          warn={(option.overtime_minutes ?? 0) > 0}
        />
      </dl>

      {option.opens_new_day && (
        <p className="text-[11.5px] text-pending">Opens a delivery day with no other work on it.</p>
      )}
      {heavy && (
        // The sentence the browser session needed and did not get: a "+1 driving minute" headline
        // must not be allowed to hide a multi-hour increase in the working day.
        <p className="text-[11.5px] leading-[1.45] text-pending">
          Cheap to drive to, expensive to serve — the extra cost is in the crew&apos;s day, not the
          road.
        </p>
      )}
      {option.reason && (
        <p className="text-[12px] leading-[1.45] text-ink-muted">{option.reason}</p>
      )}
    </Card>
  );
}

function Figure({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-[2px]">
      <dt className="text-[12px] text-ink-muted">{label}</dt>
      <dd
        className={cx(
          "font-mono text-[12px] tnum",
          warn ? "text-pending" : "text-ink-soft",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

/** "1 minute", never "1 minutes". */
export function duration(minutes: number): string {
  const n = Math.abs(minutes);
  if (n < 60) return `${n} minute${n === 1 ? "" : "s"}`;
  const hours = Math.floor(n / 60);
  const rest = n % 60;
  if (rest === 0) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${hours}h ${rest}m`;
}

function signedMinutes(minutes: number | null): string {
  if (minutes === null || minutes === 0) return "—";
  return `${minutes > 0 ? "+" : "−"}${duration(minutes)}`;
}

function signedKm(km: number | null): string {
  if (km === null || Math.abs(km) < 0.05) return "—";
  return `${km > 0 ? "+" : "−"}${Math.abs(km).toFixed(1)} km`;
}
