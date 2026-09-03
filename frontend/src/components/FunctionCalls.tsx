"use client";

import { useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Pill, cx } from "@/components/ui";
import type { AgentAction, AgentRun } from "@/lib/api";

/**
 * Every function call behind one agent run, exactly as it was persisted.
 *
 * The count is taken from the run, never written down: a hardcoded "4 function calls" beside a
 * three-call run is the kind of detail that decides whether an audience believes the rest of the
 * screen. Same reason the provider is stated rather than assumed -- the run records who actually
 * decided, so a fallback to the standard procedure says so.
 *
 * Arguments and results are shown raw. `AgentTrace.describe()` is the right rendering for the
 * feed -- "3 options, 2 feasible" reads better than an object -- but it is lossy by design: an
 * array becomes its length and a `false` disappears entirely, which is precisely what an inspector
 * must not do. Both are already sanitised server-side, at the point the log is written
 * (`planning/tools.sanitise_for_log`), so nothing here is redacted at render time and hoped for.
 *
 * What is NOT in here, and cannot be: the model's private reasoning. `reason` is a one-line
 * operational summary written for a coordinator. There is no chain-of-thought to expose.
 */
export function FunctionCallsPill({ run }: { run: AgentRun }) {
  const [open, setOpen] = useState(false);
  const count = run.actions.length;
  if (count === 0) return null;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className={cx(
          "inline-flex items-center gap-1.5 rounded-full border border-rail bg-surface px-2.5 py-1",
          "text-[11.5px] text-ink-muted transition-colors hover:bg-sunk hover:text-ink",
        )}
      >
        <span aria-hidden className="font-mono text-[11px]">&#9432;</span>
        {count} function call{count === 1 ? "" : "s"} &amp; results
      </button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Agent trace — demo inspector"
        subtitle={<ProvenanceLine run={run} />}
        width={760}
      >
        <ol className="flex flex-col gap-2">
          {run.actions.map((action) => (
            <CallSection key={action.step} action={action} />
          ))}
        </ol>
        <p className="mt-4 border-t border-rail pt-3 text-[11.5px] leading-[1.5] text-ink-faint">
          A customer never sees this panel. Every entry is a real persisted tool call with its
          arguments and its result; the reasons are one-line operational summaries, not model
          reasoning.
        </p>
      </Modal>
    </>
  );
}

/** Who chose these actions. Read off the run, so a rule-driven run cannot pose as a model one. */
function ProvenanceLine({ run }: { run: AgentRun }) {
  const byModel = run.decider === "LLMDecisionAgent";
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span>
        {byModel ? "Actions chosen by" : "Actions chosen by the"}{" "}
        <span className="font-mono text-[11.5px] text-ink-soft">
          {byModel ? (run.model_id ?? "the model") : "standard procedure"}
        </span>
      </span>
      {run.decider_error && (
        <Pill tone="alert">
          fell back: {run.decider_error.slice(0, 80)}
        </Pill>
      )}
    </span>
  );
}

function CallSection({ action }: { action: AgentAction }) {
  const [open, setOpen] = useState(false);
  const state = action.ok ? "ok" : action.error === "not_allowed" ? "refused" : "error";

  return (
    <li className="overflow-hidden rounded-[10px] border border-rail bg-surface">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 px-3.5 py-2.5 text-left hover:bg-sunk/60"
      >
        <span className="mt-[3px] font-mono text-[11px] tabular-nums text-ink-faint">
          {action.step}
        </span>
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[12.5px] text-ink">{action.tool}</span>
            <Pill tone={state === "ok" ? "locked" : state === "refused" ? "pending" : "alert"}>
              {state === "ok" ? "succeeded" : state === "refused" ? "refused" : "failed"}
            </Pill>
          </span>
          <span className="text-[12.5px] leading-[1.45] text-ink-soft">{action.summary}</span>
          {action.reason && (
            <span className="text-[11.5px] leading-[1.45] text-ink-muted">{action.reason}</span>
          )}
        </span>
        <span aria-hidden className="mt-0.5 text-[11px] text-ink-faint">
          {open ? "\u2212" : "+"}
        </span>
      </button>

      {open && (
        <div className="flex flex-col gap-2.5 border-t border-rail px-3.5 py-3">
          <Payload label="Arguments" value={action.arguments} />
          <Payload label="Result" value={action.data} />
          {action.error && (
            <p className="text-[12px] text-alert">
              <span className="font-mono text-[11px] uppercase tracking-[0.08em]">error</span>{" "}
              {action.error}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

function Payload({ label, value }: { label: string; value: Record<string, unknown> }) {
  const empty = !value || Object.keys(value).length === 0;
  const text = empty ? "" : JSON.stringify(value, null, 2);

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.11em] text-ink-faint">
          {label}
        </span>
        {!empty && (
          <button
            onClick={() => navigator.clipboard?.writeText(text)}
            className="rounded-[6px] px-1.5 py-0.5 text-[11px] text-ink-faint hover:bg-sunk hover:text-ink"
          >
            Copy
          </button>
        )}
      </div>
      {empty ? (
        <p className="text-[12px] text-ink-faint">none</p>
      ) : (
        <pre className="max-h-56 overflow-auto rounded-[8px] bg-sunk/70 px-3 py-2 font-mono text-[11.5px] leading-[1.5] text-ink-soft">
          {text}
        </pre>
      )}
    </div>
  );
}
