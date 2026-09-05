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

function mark(state: string): string {
  if (state === "done") return "✓";
  if (state === "failed") return "✕";
  if (state === "running") return "●";
  return "○";
}

function markTone(state: string): string {
  if (state === "done") return "text-locked";
  if (state === "failed") return "text-danger";
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

  const label = failed
    ? "Something went wrong — see what happened"
    : running
      ? "Thinking…"
      : `Thought for ${progress.seconds.toFixed(1)}s · ${progress.stages.length} steps`;

  return (
    <button
      onClick={onOpen}
      className={cx(
        "flex items-center gap-1.5 self-start rounded-full border px-2.5 py-1",
        "text-[12px] transition-colors",
        failed
          ? "border-danger/40 bg-danger/5 text-danger hover:bg-danger/10"
          : "border-rail bg-paper text-ink-soft hover:bg-wash hover:text-ink",
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

function Row({ stage }: { stage: AgentProgressStage }) {
  return (
    <li className="flex items-start gap-2.5 py-1.5">
      <span aria-hidden className={cx("mt-[1px] w-3 shrink-0 text-center text-[11px]", markTone(stage.state))}>
        {mark(stage.state)}
      </span>

      {/* min-w-0 is what stops a long detail from shoving the duration off the row. */}
      <div className="min-w-0 flex-1">
        <div className={cx("text-[13px] leading-tight", stage.state === "running" ? "text-ink" : "text-ink-soft")}>
          {stage.label}
        </div>
        {stage.reason && (
          <div className="mt-0.5 text-[11.5px] leading-snug text-ink-faint">{stage.reason}</div>
        )}
        {stage.detail && (
          <div className="mt-0.5 text-[11.5px] leading-snug text-ink-soft">{stage.detail}</div>
        )}
        {stage.tool && stage.tool !== "(model)" && (
          <div className="mt-0.5 font-mono text-[10px] text-ink-faint">{stage.tool}</div>
        )}
      </div>

      {/* Where the time actually went. Sub-100ms steps are noise at this scale. */}
      <span className="shrink-0 pt-[1px] font-mono text-[11px] tabular-nums text-ink-faint">
        {stage.seconds >= 0.1 ? `${stage.seconds.toFixed(1)}s` : ""}
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
        className="flex max-h-[82vh] w-full max-w-[520px] flex-col overflow-hidden rounded-xl bg-paper shadow-[var(--shadow-lift)]"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-3 border-b border-rail px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[14.5px] font-semibold text-ink">
              <span aria-hidden>🧠</span>
              What the agent did
            </div>
            <p className="mt-0.5 text-[11.5px] leading-snug text-ink-faint">
              Every step below is a real action with its real duration — not a progress animation.
            </p>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded px-2 py-1 text-[12px] text-ink-soft hover:bg-wash hover:text-ink"
          >
            Close
          </button>
        </header>

        <ul className="flex-1 divide-y divide-rail/60 overflow-y-auto px-4 py-1">
          {progress.stages.map((stage) => (
            <Row key={stage.key} stage={stage} />
          ))}
        </ul>

        {(progress.summary || failed || progress.run_id) && (
          <footer className="flex flex-col gap-2 border-t border-rail bg-wash/50 px-4 py-3">
            {progress.summary && progress.state !== "running" && (
              <p className="text-[12.5px] leading-snug text-ink">{progress.summary}</p>
            )}

            {failed && (
              <div className="flex items-center justify-between gap-3">
                <span className="text-[12px] text-danger">Failed at “{failed.label}”.</span>
                {onRetry && (
                  <button
                    onClick={() => {
                      onClose();
                      onRetry();
                    }}
                    className="rounded border border-rail bg-paper px-2.5 py-1 text-[12px] hover:bg-wash"
                  >
                    Retry
                  </button>
                )}
              </div>
            )}

            <p className="text-[11px] text-ink-faint">
              Full tool inputs and results are in the agent drawer.
              {progress.run_id && (
                <span className="ml-1 font-mono text-[10px]">{progress.run_id}</span>
              )}
            </p>
          </footer>
        )}
      </div>
    </div>
  );
}
