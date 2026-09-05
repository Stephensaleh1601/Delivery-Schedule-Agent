"use client";

/**
 * What the agent is doing, while the customer waits.
 *
 * A turn takes several seconds — the model reads the message, the search measures two routes, the
 * model picks. A spinner over that says "waiting" when the honest answer is "comparing sixteen
 * stops", so this shows the actual steps instead.
 *
 * Every row is a real backend event with a real duration. Nothing here is on a timer and nothing
 * is predicted: a stage that never ran never appears, and one that took three seconds says three
 * seconds. That is also why the model's own turn is a visible row — most of the wall clock is
 * there, and tool steps of 0.03s adding to eleven seconds reads as the screen lying.
 *
 * Observable actions only. Which tool ran, why in one plain sentence, and what it found. No
 * deliberation: that is not ours to publish and is not evidence of anything.
 */

import { useEffect, useState } from "react";

import { Pill, cx } from "@/components/ui";
import { dispatch, type AgentProgressState } from "@/lib/api";

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

function Mark({ state }: { state: string }) {
  if (state === "done") return <span className="text-locked">✓</span>;
  if (state === "failed") return <span className="text-danger">✕</span>;
  if (state === "running") return <span className="animate-pulse text-accent">●</span>;
  return <span className="text-ink-faint">○</span>;
}

/** The one-line status under the thread. Clicking it opens the trace. */
export function ProgressChip({
  progress,
  open,
  onToggle,
}: {
  progress: AgentProgressState;
  open: boolean;
  onToggle: () => void;
}) {
  const running = progress.stages.find((s) => s.state === "running");
  const failed = progress.stages.find((s) => s.state === "failed");
  const label = failed
    ? `Stopped at: ${failed.label}`
    : progress.state === "running"
      ? `Agent working · ${running?.label ?? "Starting…"}`
      : `Agent finished · ${progress.stages.length} steps in ${progress.seconds}s`;

  return (
    <button
      onClick={onToggle}
      className={cx(
        "flex w-full items-center gap-2 rounded-lg border px-2.5 py-1.5 text-left text-[12px]",
        "transition-colors",
        failed
          ? "border-danger/40 bg-danger/5 text-danger"
          : "border-rail bg-paper text-ink-soft hover:bg-wash",
      )}
    >
      <Mark state={failed ? "failed" : progress.state === "running" ? "running" : "done"} />
      <span className="flex-1 truncate">{label}</span>
      <span className="text-ink-faint">{open ? "Hide" : "Show"}</span>
    </button>
  );
}

export function ProgressPanel({
  progress,
  onRetry,
}: {
  progress: AgentProgressState;
  onRetry?: () => void;
}) {
  const failed = progress.stages.find((s) => s.state === "failed");

  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-rail bg-paper p-2.5">
      <ul className="flex flex-col gap-1">
        {progress.stages.map((stage) => (
          <li key={stage.key} className="flex gap-2 text-[12px] leading-[1.45]">
            <span className="mt-[2px] w-3 shrink-0 text-center">
              <Mark state={stage.state} />
            </span>
            <span className="flex-1">
              <span className={cx(stage.state === "running" ? "text-ink" : "text-ink-soft")}>
                {stage.label}
              </span>
              {stage.reason && (
                <span className="block text-[11px] text-ink-faint">{stage.reason}</span>
              )}
              {stage.detail && (
                <span className="block text-[11px] text-ink-soft">{stage.detail}</span>
              )}
            </span>
            {/* Where the time actually went. Sub-100ms steps are noise on this scale. */}
            <span className="shrink-0 font-mono text-[10.5px] text-ink-faint">
              {stage.seconds >= 0.1 ? `${stage.seconds.toFixed(1)}s` : ""}
            </span>
          </li>
        ))}
      </ul>

      {progress.summary && progress.state !== "running" && (
        <div className="border-t border-rail pt-1.5 text-[12px] text-ink">{progress.summary}</div>
      )}

      {failed && onRetry && (
        <div className="flex items-center justify-between border-t border-rail pt-1.5">
          <span className="text-[11.5px] text-danger">Failed at “{failed.label}”.</span>
          <button
            onClick={onRetry}
            className="rounded border border-rail px-2 py-0.5 text-[11.5px] hover:bg-wash"
          >
            Retry
          </button>
        </div>
      )}

      {progress.run_id && (
        <div className="flex items-center gap-1.5 border-t border-rail pt-1.5">
          <Pill>Trace</Pill>
          <span className="font-mono text-[10.5px] text-ink-faint">{progress.run_id}</span>
          <span className="text-[11px] text-ink-faint">
            — full inputs and results in the agent drawer
          </span>
        </div>
      )}
    </div>
  );
}
