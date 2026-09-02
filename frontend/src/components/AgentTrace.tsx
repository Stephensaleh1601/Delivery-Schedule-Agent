"use client";

import { Pill, cx } from "@/components/ui";
import type { AgentAction, AgentRun } from "@/lib/api";
import { TOOL_LABELS, formatRelative, titleCase } from "@/lib/format";

/**
 * What the agent did, as the record of it.
 *
 * Every row is a persisted tool call: the name, the one-line operational reason, the arguments it
 * was given and the result it returned. There is no narration layer and no model reasoning --
 * both because that is not what a coordinator needs, and because an autonomous loop is only
 * trustworthy if its record can embarrass it. A refused action shows as a refusal.
 *
 * Used in two places with the same data: a drawer from anywhere in the console, and a permanent
 * panel beside the customer conversation, where it *is* the point.
 */

const RUN_TONE = {
  completed: "locked",
  running: "accent",
  failed: "alert",
  step_limit_reached: "pending",
} as const;

export function RunHeader({ run, compact = false }: { run: AgentRun; compact?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className={cx("font-semibold text-ink", compact ? "text-[13px]" : "text-[14px]")}>
            {titleCase(run.event_type ?? "event")}
          </span>
          <Pill tone={RUN_TONE[run.status]}>{titleCase(run.status)}</Pill>
        </div>
        {run.final_summary && (
          <p className="max-w-[70ch] text-[12.5px] leading-[1.5] text-ink-muted">
            {run.final_summary}
          </p>
        )}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-0.5">
        <span className="font-mono text-[11px] text-ink-faint">{formatRelative(run.started_at)}</span>
        <span className="font-mono text-[11px] text-ink-faint tnum">
          {run.actions.length}/6 steps
        </span>
      </div>
    </div>
  );
}

export function ActionRow({ action, showIO = true }: { action: AgentAction; showIO?: boolean }) {
  const input = describe(action.arguments);
  const result = describe(action.data);

  return (
    <li
      className={cx(
        "flex flex-col gap-1.5 border-b border-rail/60 px-4 py-3 last:border-b-0",
        !action.ok && "bg-alert-wash/40",
      )}
    >
      <div className="flex items-baseline gap-2.5">
        <span className="font-mono text-[11px] text-ink-faint tnum">
          {String(action.step).padStart(2, "0")}
        </span>
        <span className={cx("text-[13px] font-semibold", action.ok ? "text-ink" : "text-alert")}>
          {TOOL_LABELS[action.tool] ?? titleCase(action.tool)}
        </span>
        <span className="font-mono text-[10.5px] text-ink-faint">{action.tool}</span>
        {action.error && (
          <span className="ml-auto">
            <Pill tone="alert">{action.error.replace(/_/g, " ")}</Pill>
          </span>
        )}
      </div>

      {action.reason && (
        <p className="pl-[26px] text-[12.5px] leading-[1.5] text-ink-soft">{action.reason}</p>
      )}

      {showIO && (input || result) && (
        <dl className="ml-[26px] grid grid-cols-[42px_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-[8px] bg-sunk/70 px-3 py-2">
          {input && (
            <>
              <dt className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint">in</dt>
              <dd className="truncate font-mono text-[11.5px] text-ink-muted">{input}</dd>
            </>
          )}
          {result && (
            <>
              <dt className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-faint">out</dt>
              <dd className="font-mono text-[11.5px] leading-[1.45] text-ink-soft">{result}</dd>
            </>
          )}
        </dl>
      )}

      <p className="pl-[26px] text-[12.5px] leading-[1.5] text-ink-muted">{action.summary}</p>
    </li>
  );
}

export function RunCard({ run, showIO = true }: { run: AgentRun; showIO?: boolean }) {
  return (
    <li className="overflow-hidden rounded-[14px] border border-rail bg-surface">
      <div className="border-b border-rail px-4 py-3">
        <RunHeader run={run} />
      </div>
      <ol className="flex flex-col">
        {run.actions.map((action) => (
          <ActionRow key={action.step} action={action} showIO={showIO} />
        ))}
      </ol>
    </li>
  );
}

/** A compact, readable rendering of a tool's input or result.
 *
 *  Deliberately not raw JSON: a coordinator reading "3 options, 2 feasible" learns something,
 *  where a pretty-printed object just looks like a leak. Long values are already truncated and
 *  personal fields already redacted server-side; this is about legibility, not safety. */
function describe(payload: Record<string, unknown> | undefined): string | null {
  if (!payload || Object.keys(payload).length === 0) return null;

  const parts: string[] = [];
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      parts.push(`${humanise(key)}: ${value.length}`);
    } else if (typeof value === "object") {
      parts.push(humanise(key));
    } else if (typeof value === "boolean") {
      if (value) parts.push(humanise(key));
    } else {
      const text = String(value);
      parts.push(`${humanise(key)}: ${text.length > 60 ? `${text.slice(0, 60)}…` : text}`);
    }
  }
  return parts.length ? parts.join(" · ") : null;
}

function humanise(key: string): string {
  return key.replace(/_/g, " ").replace(/\bid\b/, "id");
}
