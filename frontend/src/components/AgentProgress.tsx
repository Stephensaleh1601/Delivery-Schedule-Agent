"use client";

/**
 * What the agent is doing, while the customer waits.
 *
 * A turn takes several seconds — the model reads the message, the search measures two routes, the
 * model picks. A spinner over that says "waiting" when the honest answer is "comparing sixteen
 * stops", so this shows the actual steps instead.
 *
 * Two pieces, deliberately. A small chip sits under the thread and says only that work is
 * happening; the detail lives in an overlay you open. The detail used to render inline in the
 * phone column, where forty characters of reason wrapped under a label and the rows collided into
 * each other — a panel about being legible that was not.
 *
 * Every row is a real backend event with a real duration. Nothing is on a timer and nothing is
 * predicted: a stage that never ran never appears, and one that took three seconds says three
 * seconds. That is also why the model's own turn is a visible row — most of the wall clock is
 * there, and tool steps of 0.03s adding to nine seconds reads as the screen lying.
 *
 * Observable actions only. Which tool ran, why in one plain sentence, and what it found. Not
 * deliberation — that is not ours to publish, and it is not evidence of anything.
 */

import { useEffect, useState } from "react";

import { cx } from "@/components/ui";
import { dispatch, type AgentProgressState, type AgentProgressStage } from "@/lib/api";

/** Slow enough not to hammer the server, quick enough that stages do not appear in clumps. */
const POLL_MS = 700;

export function useAgentProgress(orderId: string | null, active: boolean) {
  const [progress, setProgress] = useState<AgentProgressState | null>(null);

  useEffect(() => {
    if (!orderId) return;
    let cancelled = false;

    async function tick() {
      try {
        const next = await dispatch.progress(orderId!);
        if (!cancelled) setProgress(next);
      } catch {
        /* a dropped poll is not worth surfacing -- the next one will land */
      }
    }

    void tick();
    if (!active) return;
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [orderId, active]);

  return progress;
}

/** Which headline step a raw stage belongs to.
 *
 *  The backend reports finely -- five separate "deciding what to do next" rows, four phases
 *  inside one search -- because that is what honestly happened. Seventeen rows is not what a
 *  person wants to read, so the UI groups them into the shape of the work. Nothing is dropped:
 *  a decision's seconds are charged to the step it decided on, because deciding to read the
 *  policy is part of reading the policy.
 */
const GROUPS: Array<{ id: string; label: string; keys: (key: string) => boolean }> = [
  { id: "read", label: "Understood your request", keys: (k) => k === "understanding" },
  {
    id: "noted",
    label: "Noted what you told us",
    keys: (k) => k === "record_availability" || k === "record_rejection",
  },
  { id: "policy", label: "Read the delivery policy", keys: (k) => k === "retrieve_policy" },
  { id: "routes", label: "Loaded the published routes", keys: (k) => k === "get_existing_routes" },
  {
    id: "search",
    label: "Searched both routes for a place to fit you",
    keys: (k) =>
      ["find_insertion_options", "nearby", "positions", "timing", "ranking"].includes(k),
  },
  {
    id: "offer",
    label: "Chose what to offer",
    keys: (k) => k === "create_normal_offer" || k === "create_alternative_offer",
  },
  { id: "explain", label: "Explained the choice", keys: (k) => k === "explain_choice" },
  { id: "ask", label: "Asked you a question", keys: (k) => k === "ask_clarification" },
  { id: "escalate", label: "Handed over to a coordinator", keys: (k) => k === "create_exception" },
  { id: "confirm", label: "Confirmed your booking", keys: (k) => k === "lock_appointment" },
  { id: "reply", label: "Sent the reply", keys: (k) => k === "send_message" },
];

export interface Headline {
  id: string;
  label: string;
  detail: string;
  seconds: number;
  state: "running" | "done" | "failed";
}

export function summarise(stages: AgentProgressStage[]): Headline[] {
  const out: Headline[] = [];
  // Time spent by the model choosing the next action. Charged forward to whatever it chose --
  // shown on its own it is five identical rows that tell a reader nothing.
  let thinking = 0;

  for (const stage of stages) {
    if (stage.key.startsWith("decide-")) {
      thinking += stage.seconds;
      continue;
    }
    // Falls back to the stage's own label rather than vanishing. A step nobody has grouped yet
    // is still a step that happened, and dropping it makes the count lie.
    const group = GROUPS.find((g) => g.keys(stage.key)) ?? {
      id: stage.key,
      label: stage.label,
      keys: () => false,
    };

    const existing = out.find((h) => h.id === group.id);
    if (existing) {
      existing.seconds += stage.seconds + thinking;
      // The last phase to report something wins: inside the search that is the ranking, which
      // carries the count a reader actually wants.
      if (stage.detail) existing.detail = stage.detail;
      if (stage.state !== "done") existing.state = stage.state;
    } else {
      out.push({
        id: group.id,
        label: group.label,
        detail: stage.detail,
        seconds: stage.seconds + thinking,
        state: stage.state,
      });
    }
    thinking = 0;
  }

  // A turn that ended while the model was still choosing still has to account for that time.
  if (thinking > 0 && out.length > 0) out[out.length - 1].seconds += thinking;
  return out;
}

function mark(state: string): string {
  if (state === "done") return "✓";
  if (state === "failed") return "✕";
  if (state === "running") return "●";
  return "○";
}

function markTone(state: string): string {
  if (state === "done") return "text-locked";
  if (state === "failed") return "text-alert";
  if (state === "running") return "text-accent animate-pulse";
  return "text-ink-faint";
}

/** The one-line status under the thread. Says that work is happening, and nothing else. */
export function ThinkingChip({
  progress,
  onOpen,
}: {
  progress: AgentProgressState;
  onOpen: () => void;
}) {
  const running = progress.state === "running";
  const failed = progress.stages.some((s) => s.state === "failed");
  const steps = summarise(progress.stages);

  const label = failed
    ? "Something went wrong — see what happened"
    : running
      ? "Thinking…"
      : `Thought for ${progress.seconds.toFixed(1)}s · ${steps.length} steps`;

  return (
    <button
      onClick={onOpen}
      className={cx(
        "flex items-center gap-1.5 self-start rounded-full border px-2.5 py-1",
        "text-[12px] transition-colors",
        failed
          ? "border-alert-edge bg-alert-wash text-alert hover:bg-alert-wash"
          : "border-rail bg-surface text-ink-soft hover:bg-sunk hover:text-ink",
      )}
      title="See what the agent did"
    >
      <span aria-hidden className={cx("text-[13px]", running && "animate-pulse")}>
        🧠
      </span>
      <span>{label}</span>
    </button>
  );
}

function Row({ step }: { step: Headline }) {
  return (
    <li className="flex items-start gap-2.5 py-2">
      <span
        aria-hidden
        className={cx("mt-[2px] w-3 shrink-0 text-center text-[11px]", markTone(step.state))}
      >
        {mark(step.state)}
      </span>

      {/* min-w-0 is what stops a long finding from shoving the duration off the row. */}
      <div className="min-w-0 flex-1">
        <div
          className={cx(
            "text-[13px] leading-tight",
            step.state === "running" ? "text-ink" : "text-ink-soft",
          )}
        >
          {step.label}
        </div>
        {step.detail && (
          <div className="mt-1 text-[11.5px] leading-snug text-ink-faint">{step.detail}</div>
        )}
      </div>

      {/* Where the time actually went. Sub-100ms steps are noise at this scale. */}
      <span className="shrink-0 pt-[2px] font-mono text-[11px] tabular-nums text-ink-faint">
        {step.seconds >= 0.1 ? `${step.seconds.toFixed(1)}s` : ""}
      </span>
    </li>
  );
}

export function ThinkingOverlay({
  progress,
  onClose,
  onRetry,
}: {
  progress: AgentProgressState;
  onClose: () => void;
  onRetry?: () => void;
}) {
  const failed = progress.stages.find((s) => s.state === "failed");

  // Escape closes it, like every other overlay a judge has used.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-6"
      onClick={onClose}
    >
      <div
        className="flex max-h-[82vh] w-full max-w-[520px] flex-col overflow-hidden rounded-xl bg-surface shadow-[var(--shadow-lift)]"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-rail px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[14.5px] font-semibold text-ink">
              <span aria-hidden>🧠</span>
              What the agent did
            </div>
            <p className="mt-0.5 text-[11.5px] leading-snug text-ink-faint">
              Real steps with real timings, not an animation. Full tool inputs and results are in
              the agent drawer.
            </p>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded px-2 py-1 text-[12px] text-ink-soft hover:bg-sunk hover:text-ink"
          >
            Close
          </button>
        </header>

        <ul className="flex-1 divide-y divide-rail/60 overflow-y-auto px-4 py-1">
          {summarise(progress.stages).map((step) => (
            <Row key={step.id} step={step} />
          ))}
        </ul>

        {(progress.summary || failed || progress.run_id) && (
          <footer className="flex flex-col gap-2 border-t border-rail bg-sunk/60 px-4 py-3">
            {progress.summary && progress.state !== "running" && (
              <p className="text-[12.5px] leading-snug text-ink">{progress.summary}</p>
            )}

            {failed && (
              <div className="flex items-center justify-between gap-3">
                <span className="text-[12px] text-alert">Failed at “{failed.label}”.</span>
                {onRetry && (
                  <button
                    onClick={() => {
                      onClose();
                      onRetry();
                    }}
                    className="rounded border border-rail bg-surface px-2.5 py-1 text-[12px] hover:bg-sunk"
                  >
                    Retry
                  </button>
                )}
              </div>
            )}

            {progress.run_id && (
              <p className="font-mono text-[10px] text-ink-faint">{progress.run_id}</p>
            )}
          </footer>
        )}
      </div>
    </div>
  );
}
