"use client";

/**
 * What the driver receives, shown in the same WhatsApp chrome the customer thread uses.
 *
 * Simulated, and labelled as such — the phone frame carries a "simulated" badge. Nothing leaves
 * the machine; the point is to show the last mile of the workflow, not to integrate a messaging
 * provider for a demo.
 *
 * The Maps link is Google's public directions URL scheme: no API key, no OAuth, no account to
 * connect. A driver opens it on their own phone and gets turn-by-turn navigation through every
 * stop in the order the solver chose.
 */

import { Bubble, Phone, Wallpaper, chatTime } from "@/components/WhatsApp";
import { Eyebrow, Pill } from "@/components/ui";
import type { DriverDispatch } from "@/lib/api";
import { formatDate } from "@/lib/format";

export function DriverPanel({ sent, onClose }: { sent: DriverDispatch; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-6">
      <div className="flex max-h-full w-full max-w-[420px] flex-col gap-3 overflow-y-auto">
        <div className="flex items-center justify-between">
          <Eyebrow>Sent to driver</Eyebrow>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-[12px] text-paper/80 hover:text-paper"
          >
            Close
          </button>
        </div>

        <Phone
          name={sent.driver.name}
          initials={sent.driver.name.split(" ").map((w) => w[0]).join("").slice(0, 2)}
          status={`Driver · ${sent.driver.phone}`}
        >
          <Wallpaper>
            <Bubble from="me" time={chatTime()}>
              <span className="whitespace-pre-wrap">{sent.message}</span>
            </Bubble>
          </Wallpaper>
        </Phone>

        <div className="flex flex-wrap items-center gap-2 rounded-lg bg-paper p-3">
          <Pill tone="locked">{`Route v${sent.plan_version}`}</Pill>
          <Pill>{`${sent.stop_count} stops`}</Pill>
          <Pill>{formatDate(sent.date)}</Pill>
          {/* A real link, opened in a new tab: a judge can click it and see the actual route. */}
          <a
            href={sent.maps_url}
            target="_blank"
            rel="noreferrer"
            className="text-[12.5px] font-medium text-accent underline underline-offset-2"
          >
            Open in Google Maps
          </a>
        </div>
      </div>
    </div>
  );
}
