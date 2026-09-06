/**
 * Formatting shared across screens.
 *
 * Deliberately hand-rolled rather than strftime-style: the backend hit exactly this problem with
 * %-d being POSIX-only, and the browser equivalents (toLocaleDateString) vary by the viewer's
 * locale, which would make a recorded demo look different on someone else's machine.
 */

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Parse an ISO date as a local calendar date, not a UTC instant. `new Date("2026-09-04")` is
 *  midnight UTC, which renders as the previous day west of Greenwich. */
export function parseDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** "Friday, 4 September" */
export function formatDate(iso: string): string {
  const d = parseDate(iso);
  return `${WEEKDAYS[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

/** "Fri 4 Sep" */
export function formatDateShort(iso: string): string {
  const d = parseDate(iso);
  return `${WEEKDAYS[d.getDay()].slice(0, 3)} ${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}`;
}

/** "Fri" / "4" split, for the horizon strip. */
export function dayParts(iso: string): { weekday: string; day: number; month: string } {
  const d = parseDate(iso);
  return {
    weekday: WEEKDAYS[d.getDay()].slice(0, 3),
    day: d.getDate(),
    month: MONTHS[d.getMonth()].slice(0, 3),
  };
}

/** "9:00am" from "09:00" */
export function formatTime(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  const hour = h % 12 || 12;
  return `${hour}:${String(m).padStart(2, "0")}${h < 12 ? "am" : "pm"}`;
}

export function formatWindow(w: { start: string; end: string }): string {
  return `${formatTime(w.start)}–${formatTime(w.end)}`;
}

/** "1h 46m" — drive totals read better than a bare minute count once they pass an hour. */
export function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h ${m}m`;
}

/** "+12m" / "−30m" / "no change" — a delta the reader does not have to compute. */
export function formatDelta(minutes: number): string {
  if (minutes === 0) return "no change";
  return `${minutes > 0 ? "+" : "−"}${formatDuration(Math.abs(minutes))}`;
}

export function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function titleCase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Names the tools by what a coordinator would call them, not by their function name. */
export const TOOL_LABELS: Record<string, string> = {
  evaluate_slots: "Checked availability",
  create_offer: "Prepared offer",
  send_message: "Messaged customer",
  lock_appointment: "Locked appointment",
  record_rejection: "Recorded decline",
  replan_day: "Rebuilt the day",
  find_ready_replacements: "Searched for replacements",
  create_exception: "Escalated to a coordinator",
  finish: "Finished",
};

export const STATUS_LABELS: Record<string, string> = {
  pending_availability: "Needs availability",
  pending_planning: "Awaiting planning",
  offered: "Offer sent",
  confirmed: "Confirmed",
  sequenced: "On the route",
  dispatched: "Out for delivery",
  completed: "Delivered",
  cancelled: "Cancelled",
  exception: "Needs a coordinator",
};

/** What the customer ordered, in words a coordinator would use.
 *
 * The furniture values are still in the backend enum so rows written before the business changed
 * still validate when read back -- they are never offered, but a legacy row must not render as a
 * raw identifier if one surfaces. Anything unrecognised falls through to title case.
 */
export const ORDER_LABELS: Record<string, string> = {
  pet_food_box: "Subscription box",
  one_off_pet_order: "One-off order",
  other: "Other",
  sofa: "Sofa (legacy)",
  bed: "Bed (legacy)",
  cabinet: "Cabinet (legacy)",
};

export function orderLabel(jobType: string): string {
  return ORDER_LABELS[jobType] ?? titleCase(jobType);
}
