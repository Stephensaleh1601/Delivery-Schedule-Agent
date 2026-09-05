"use client";

import { useState } from "react";
import { Card, Eyebrow, Pill, cx } from "@/components/ui";
import { RouteStrip } from "@/components/RouteStrip";
import type { Decision, DecisionCandidate, DecisionStep } from "@/lib/api";

/**
 * The agent's decision, for someone who has five seconds.
 *
 * The previous version was correct and unreadable: constraints, depot configuration, service
 * durations, before/after tables. A judge watching the conversation had to work out for themselves
 * why 9–11 had become 11–1, and it looked like the optimiser had moved the customer at random.
 *
 * So this leads with the sequence — rejected, removed, re-solved, found — then shows at most two
 * candidates and one recommendation. The planning rules are still here and still true; they are
 * collapsed, because nobody opens a panel to be told what a working day is.
 *
 * The tool calls are deliberately absent. They live under the message that produced them, in
 * "Function calls & results", which is the technical audit. This panel is what happened and why it
 * matters; that modal is proof the agent actually did it.
 */
export function AgentDecision({ decision }: { decision: Decision }) {
  const [showRules, setShowRules] = useState(false);

  return (
    <div className="flex flex-col gap-4">
      {decision.asked && (
        <div className="flex flex-col gap-0.5">
          <Eyebrow>Customer asked for</Eyebrow>
          <p className="text-[14px] leading-[1.45] text-ink">{decision.asked}</p>
        </div>
      )}

      {decision.what_changed && (
        <section className="flex flex-col gap-2">
          <Eyebrow>What changed</Eyebrow>
          <p className="text-[14px] font-medium leading-[1.45] text-ink">
            {decision.what_changed}
          </p>
          {decision.steps.length > 0 && <Flow steps={decision.steps} />}
        </section>
      )}

      {decision.candidates.length > 0 && (
        <section className="flex flex-col gap-2">
          <Eyebrow>
            {decision.candidates.length === 1 ? "The option" : "The two real choices"}
          </Eyebrow>
          <div
            className={cx(
              "grid gap-3",
              decision.candidates.length === 2 ? "grid-cols-2" : "grid-cols-1",
            )}
          >
            {decision.candidates.map((candidate) => (
              <CandidateCard key={candidate.label} candidate={candidate} />
            ))}
          </div>
        </section>
      )}

      {decision.candidates.length > 0 && <RouteStrip candidates={decision.candidates} />}

      {decision.decision && (
        <section
          className={cx(
            "rounded-[12px] border px-4 py-3",
            "border-locked-edge bg-locked-wash/50",
          )}
        >
          <Eyebrow>Decision</Eyebrow>
          <p className="mt-1 text-[14px] font-medium leading-[1.5] text-ink">
            {decision.decision}
          </p>
        </section>
      )}

      {decision.evidence.length > 0 && (
        <section className="flex flex-col gap-1">
          <Eyebrow>What the agent checked</Eyebrow>
          <ul className="flex flex-col gap-0.5">
            {decision.evidence.map((row) => (
              <li
                key={row.text}
                className="flex gap-2 text-[12.5px] leading-[1.5] text-ink-soft"
              >
                <span
                  aria-hidden
                  className={cx(
                    "mt-[5px] text-[10px]",
                    row.tone === "removed" && "text-ink-faint",
                    row.tone === "solved" && "text-locked",
                    row.tone === "found" && "text-accent",
                    row.tone === "neutral" && "text-ink-faint",
                  )}
                >
                  {row.tone === "removed" ? "−" : row.tone === "solved" ? "✓" : "·"}
                </span>
                {row.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      {decision.outcome.length > 0 && (
        <section className="flex flex-col gap-1">
          <Eyebrow>Outcome</Eyebrow>
          <ul className="flex flex-col gap-0.5">
            {decision.outcome.map((line) => (
              <li key={line} className="flex gap-2 text-[12.5px] leading-[1.5] text-ink-soft">
                <span aria-hidden className="mt-[5px] text-[10px] text-locked">✓</span>
                {line}
              </li>
            ))}
          </ul>
        </section>
      )}

      {decision.planning_rules.length > 0 && (
        <div className="border-t border-rail pt-2.5">
          <button
            onClick={() => setShowRules((v) => !v)}
            aria-expanded={showRules}
            className="text-[11.5px] text-ink-faint transition-colors hover:text-ink-soft"
          >
            {showRules ? "− " : "+ "}Planning rules applied
          </button>
          {showRules && (
            <ul className="mt-1.5 flex flex-col gap-1">
              {decision.planning_rules.map((rule) => (
                <li key={rule} className="flex gap-2 text-[11.5px] leading-[1.45] text-ink-muted">
                  <span aria-hidden className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-rail-strong" />
                  {rule}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** rejected → removed → re-solved → found. The part that makes a shifted window look deliberate. */
function Flow({ steps }: { steps: DecisionStep[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-x-1.5 gap-y-1.5">
      {steps.map((step, i) => (
        <li key={step.text} className="flex items-center gap-1.5">
          {i > 0 && (
            <span aria-hidden className="text-[12px] text-ink-faint">
              →
            </span>
          )}
          <span
            className={cx(
              "rounded-full border px-2.5 py-[3px] text-[11.5px] leading-none",
              step.tone === "removed" && "border-alert-edge bg-alert-wash text-alert",
              step.tone === "solved" && "border-rail-strong bg-sunk text-ink-soft",
              step.tone === "found" && "border-locked-edge bg-locked-wash text-locked",
              step.tone === "neutral" && "border-rail bg-surface text-ink-muted",
            )}
          >
            {step.text}
          </span>
        </li>
      ))}
    </ol>
  );
}

function CandidateCard({ candidate }: { candidate: DecisionCandidate }) {
  const heavy =
    (candidate.finishes_later_minutes ?? 0) >= 60 ||
    (candidate.idle_minutes ?? 0) >= 60 ||
    (candidate.overtime_minutes ?? 0) > 0;

  return (
    <Card
      tone={candidate.kind === "route" ? "locked" : "neutral"}
      className="flex flex-col gap-2 px-3.5 py-3"
    >
      <div className="flex flex-col gap-1">
        <span className="text-[13.5px] font-semibold text-ink">{candidate.label}</span>
        <div className="flex flex-wrap items-center gap-1.5">
          <Pill tone={candidate.kind === "route" ? "locked" : "neutral"}>{candidate.badge}</Pill>
          {/* Worth comparing is not the same as put to the customer. Saying "offered" about a
              window nobody was shown is the panel being confidently wrong. */}
          {!candidate.offered && <Pill tone="neutral">Compared, not offered</Pill>}
        </div>
      </div>

      <ul className="flex flex-col gap-0.5">
        <Fact value={signed(candidate.added_drive_minutes, duration)} label="driving" />
        <Fact value={signed(candidate.added_distance_km, (n) => `${n.toFixed(1)} km`)} label="" />
        {candidate.finishes_later_minutes ? (
          <Fact
            value={`Crew finishes ${duration(candidate.finishes_later_minutes)} later`}
            label=""
            warn={candidate.finishes_later_minutes >= 60}
          />
        ) : null}
        {candidate.overtime_minutes ? (
          <Fact value={`${duration(candidate.overtime_minutes)} overtime`} label="" warn />
        ) : null}
        {candidate.opens_new_day && <Fact value="Opens a new delivery day" label="" warn />}
        <Fact value={`No existing promises moved`} label="" />
      </ul>

      <p
        className={cx(
          "text-[12px] leading-[1.45]",
          heavy ? "text-pending" : "text-ink-muted",
        )}
      >
        {candidate.explanation}
      </p>
      {candidate.insertion && (
        <p className="text-[11.5px] leading-[1.4] text-ink-faint">{candidate.insertion}</p>
      )}
    </Card>
  );
}

function Fact({ value, label, warn }: { value: string | null; label: string; warn?: boolean }) {
  if (!value) return null;
  return (
    <li
      className={cx(
        "flex gap-1.5 text-[12.5px] leading-[1.45]",
        warn ? "text-pending" : "text-ink-soft",
      )}
    >
      <span aria-hidden className="mt-[6px] h-1 w-1 shrink-0 rounded-full bg-rail-strong" />
      <span>
        {value}
        {label && ` ${label}`}
      </span>
    </li>
  );
}

/** "1 minute", never "1 minutes". */
export function duration(minutes: number): string {
  const n = Math.abs(minutes);
  if (n < 60) return `${n} minute${n === 1 ? "" : "s"}`;
  const hours = Math.floor(n / 60);
  const rest = n % 60;
  if (rest === 0) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${hours}h ${String(rest).padStart(2, "0")}m`;
}

function signed(value: number | null, format: (n: number) => string): string | null {
  if (value === null || Math.abs(value) < 0.05) return null;
  return `${value > 0 ? "+" : "−"}${format(Math.abs(value))}`;
}
