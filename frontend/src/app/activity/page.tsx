"use client";

import { Page } from "@/components/Shell";
import {
  Button,
  Card,
  EmptyPanel,
  ErrorPanel,
  Eyebrow,
  LoadingPanel,
  Pill,
  SectionHeader,
  cx,
} from "@/components/ui";
import { dispatch, type AgentRun } from "@/lib/api";
import { TOOL_LABELS, formatRelative, titleCase } from "@/lib/format";
import { useResource } from "@/lib/useResource";

/**
 * What the agent actually did.
 *
 * Every row here is a persisted tool call and its typed result -- there is no narration layer.
 * A refused action shows as a refusal, a step limit shows as a step limit. That is the point:
 * an autonomous loop is only trustworthy if its record can embarrass it.
 */
export default function ActivityPage() {
  const runs = useResource(() => dispatch.agentRuns(30), []);

  if (runs.error) {
    return (
      <Page title="Agent activity" lede="Every decision, and what it actually did.">
        <ErrorPanel error={runs.error} onRetry={runs.reload} />
      </Page>
    );
  }

  const list = runs.data ?? [];
  const totalSteps = list.reduce((n, r) => n + r.actions.length, 0);
  const refusals = list.reduce(
    (n, r) => n + r.actions.filter((a) => a.error === "action_not_allowed").length,
    0,
  );

  return (
    <Page
      title="Agent activity"
      lede="The model chooses which action to take and explains why in one sentence. These tools decide what is true — it never calculates a drive time or an appointment window itself."
      actions={
        <Button variant="secondary" onClick={runs.reload} busy={runs.loading && !runs.initialising}>
          Refresh
        </Button>
      }
    >
      {runs.initialising ? (
        <LoadingPanel rows={3} label="Loading agent runs" />
      ) : list.length === 0 ? (
        <EmptyPanel
          title="The agent hasn't run yet"
          description="Book a delivery from the Conversation screen and the whole loop — evaluate, offer, message — will be recorded here."
        />
      ) : (
        <>
          <div className="enter flex flex-wrap items-center gap-x-6 gap-y-2 rounded-[14px] border border-rail bg-surface px-5 py-3.5">
            <Figure label="Runs" value={list.length} />
            <Figure label="Tool calls" value={totalSteps} />
            <Figure label="Step cap" value="6 per event" />
            <Figure
              label="Refused actions"
              value={refusals}
              tone={refusals > 0 ? "alert" : "neutral"}
            />
            <p className="ml-auto max-w-[38ch] text-[12px] leading-[1.45] text-ink-muted">
              An action outside the approved list is refused and logged, never executed.
            </p>
          </div>

          <ol className="flex flex-col gap-3">
            {list.map((run, i) => (
              <RunCard key={run.id} run={run} delay={i * 35} />
            ))}
          </ol>
        </>
      )}
    </Page>
  );
}

function Figure({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "alert";
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
        {label}
      </span>
      <span
        className={cx(
          "font-display text-[19px] leading-none tnum",
          tone === "alert" ? "text-alert" : "text-ink",
        )}
      >
        {value}
      </span>
    </div>
  );
}

const RUN_TONE = {
  completed: "locked",
  running: "accent",
  failed: "alert",
  step_limit_reached: "pending",
} as const;

function RunCard({ run, delay }: { run: AgentRun; delay: number }) {
  const tone = RUN_TONE[run.status];

  return (
    <Card as="li" className="enter overflow-hidden p-0" style={{ animationDelay: `${delay}ms` }}>
      <div className="flex items-start justify-between gap-4 border-b border-rail px-5 py-3.5">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-semibold text-ink">
              {titleCase(run.event_type ?? "event")}
            </span>
            <Pill tone={tone}>{titleCase(run.status)}</Pill>
          </div>
          <p className="max-w-[74ch] text-[13px] leading-[1.5] text-ink-muted">
            {run.final_summary || "No summary recorded."}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-0.5">
          <span className="font-mono text-[11px] text-ink-faint">
            {formatRelative(run.started_at)}
          </span>
          <span className="font-mono text-[11px] text-ink-faint tnum">
            {run.actions.length}/6 steps
          </span>
        </div>
      </div>

      <ol className="flex flex-col">
        {run.actions.map((action) => (
          <li
            key={action.step}
            className={cx(
              "grid grid-cols-[34px_190px_minmax(0,1fr)] items-start gap-4 border-b border-rail/60 px-5 py-3 last:border-b-0",
              !action.ok && "bg-alert-wash/40",
            )}
          >
            <span className="pt-[3px] font-mono text-[11px] text-ink-faint tnum">
              {String(action.step).padStart(2, "0")}
            </span>

            <div className="flex flex-col gap-0.5">
              <span
                className={cx(
                  "text-[13px] font-medium",
                  action.ok ? "text-ink" : "text-alert",
                )}
              >
                {TOOL_LABELS[action.tool] ?? titleCase(action.tool)}
              </span>
              <span className="font-mono text-[10.5px] text-ink-faint">{action.tool}</span>
            </div>

            <div className="flex flex-col gap-1">
              <span className="text-[13px] leading-[1.5] text-ink-soft">{action.summary}</span>
              {action.reason && (
                <span className="text-[12px] leading-[1.45] text-ink-faint">
                  <span className="font-mono text-[10px] uppercase tracking-[0.1em]">why</span>{" "}
                  {action.reason}
                </span>
              )}
              {action.error && (
                <Pill tone="alert">{action.error.replace(/_/g, " ")}</Pill>
              )}
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}
