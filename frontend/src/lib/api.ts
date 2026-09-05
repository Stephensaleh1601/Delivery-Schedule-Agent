/**
 * The only place this app talks to the backend.
 *
 * Everything goes through a relative /api path, which next.config.ts proxies to FastAPI. That
 * keeps the browser on one origin (no CORS middleware to configure) and means no build-time
 * base URL to get wrong.
 *
 * Errors are surfaced, never swallowed into a plausible-looking empty state. A screen that
 * cannot reach the backend says so; it does not render zeros that look like real figures.
 */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "Can't reach the dispatch service. Is the FastAPI server running on port 8000?",
      0,
    );
  }

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = await response.json();
      detail = typeof body?.detail === "string" ? body.detail : undefined;
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    throw new ApiError(detail ?? `Request failed (${response.status})`, response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
};

// -- Response shapes -----------------------------------------------------------
// Mirrors what dispatch_agent/webapp/main.py returns. `npm run gen:api` regenerates the full
// schema from /openapi.json into api-types.ts; these are the narrowed views the screens use.

export interface Horizon {
  today: string;
  first: string;
  last: string;
  dates: string[];
}

export interface Bootstrap {
  horizon: Horizon;
  map: { google_maps_api_key: string; depot: { lat: number; lng: number; address: string } };
  operating: {
    work_day_start: string;
    work_day_end: string;
    soft_day_end: string;
    day_opening_penalty_minutes: number;
    preference_penalty_per_rank: number;
    routing_provider: string;
  };
}

export type PlanningStatus =
  | "pending_availability"
  | "pending_planning"
  | "offered"
  | "confirmed"
  | "sequenced"
  | "dispatched"
  | "completed"
  | "cancelled"
  | "exception";

export type ReadinessStatus = "ready" | "delayed" | "cancelled";

export interface Window {
  start: string;
  end: string;
}

export interface AvailabilityOption {
  id: string;
  date: string;
  start: string;
  end: string;
  preference_rank: number;
}

export interface Order {
  id: string;
  customer_name: string;
  phone: string | null;
  address: string;
  postal_code: string | null;
  job_type: string;
  delivery_date: string | null;
  availability: Window[];
  availability_options: AvailabilityOption[];
  locked_window: Window | null;
  duration_minutes: number;
  planning_status: PlanningStatus;
  readiness_status: ReadinessStatus;
  can_deliver_early: boolean;
  notes: string | null;
}

export interface ScoreBreakdown {
  incremental_drive_minutes: number;
  day_opening_penalty_minutes: number;
  preference_penalty_minutes: number;
  overtime_penalty_minutes: number;
}

/** Before/after for one candidate date. Route efficiency is the objective; preference is a
 *  courtesy weight and deliberately lives in `breakdown`, not here. */
export interface RouteImpact {
  drive_minutes: { before: number; after: number };
  distance_km: { before: number; after: number };
  stops: { before: number; after: number };
  /** Real minutes worked past the soft day end -- a duration, unlike the penalties. */
  overtime_minutes: number;
  /** Door to door: how long the crew is actually out. */
  working_span_minutes: { before: number; after: number };
  /** Out, not driving, not delivering. A van parked outside a block for three hours has driven
   *  nowhere, which is exactly why this cannot be inferred from any driving figure. */
  idle_minutes: { before: number; after: number };
  /** `before` is null when the day had no stops -- it did not exist yet. */
  finishes_at: { before: string | null; after: string };
  opens_empty_day: boolean;
  empty_day_overhead_minutes: number;
  /** Broad region from the postal district table -- a fact about the address, not a guess. */
  region: string | null;
  position: number;
  stop_count: number;
  /** Built deterministically from the solved route. The customer one names no other customer. */
  customer_reason: string | null;
  coordinator_reason: string | null;
  preference_rank: number;
}

export interface Evaluation {
  availability_option_id: string;
  date: string;
  /** What the customer said they could do -- often a whole day. */
  window: Window;
  /** The narrow window we would offer, derived from the arrival the solver chose. Null if infeasible. */
  promise_window: Window | null;
  /** This order's own solved stop: arrival to departure. Null if infeasible. */
  service_window: Window | null;
  feasible: boolean;
  infeasible_reason: string | null;
  /**
   * A RANKING INDEX with no unit -- it mixes real driving minutes with artificial penalties (the
   * empty-day charge is a planning weight nobody drives). Never render it as a time, a cost, or a
   * headline figure; show `route_impact`'s components instead.
   */
  total_score: number | null;
  breakdown: ScoreBreakdown;
  route_impact: RouteImpact;
  baseline_drive_minutes: number;
  proposed_drive_minutes: number;
}

export interface OfferSlot {
  id: string;
  /** The customer's original request this slot came from -- lets the UI show its evaluation. */
  availability_option_id: string;
  date: string;
  window: Window;
  /** Why this time, from the solved route. Names no other customer. */
  reason: string | null;
  label: string;
}

export interface Offer {
  id: string;
  order_id: string;
  round_number: number;
  status: string;
  accepted_slot_id: string | null;
  options: OfferSlot[];
}

export interface PlanOptions {
  offer: Offer | null;
  message?: string | null;
  evaluations: Evaluation[];
  /** True when an offer was already outstanding, so no new one was created. */
  reused?: boolean;
  run?: AgentRun | null;
  error: string | null;
}

/** What was sent to a driver, and when. `plan_version` is read at send time, so a route sent
 *  before a late booking cannot silently be the stale one. */
export interface DriverDispatch {
  date: string;
  driver: { id: string; name: string; phone: string };
  plan_version: number;
  plan_id: string;
  stop_count: number;
  maps_url: string;
  message: string;
  sent_at: string;
}

export interface PlanVersion {
  id: string;
  delivery_date: string;
  version: number;
  status: "draft" | "active" | "superseded";
  reason_created: string;
  parent_plan_id: string | null;
  stop_count: number;
  total_drive_minutes: number;
  return_drive_minutes: number;
  round_trip_drive_minutes: number;
  total_distance_km: number;
  return_distance_km: number;
  round_trip_distance_km: number;
  /** False for plans published before distance was recorded. Show "not recorded", not 0 km. */
  distance_recorded: boolean;
  finishes_at: string | null;
  /** Minutes since midnight as well as the clock string, so a real delta can be shown. The route
   *  page printed an em-dash where "+3h 46m" belonged, because "17:34" cannot be subtracted. */
  completion_minutes: number | null;
  working_span_minutes: number;
  idle_minutes: number;
  service_minutes: number;
  generated_at: string;
}

export interface PlanStop {
  sequence_index: number;
  job_id: string;
  customer_name: string;
  address: string | null;
  postal_code: string | null;
  job_type: string | null;
  duration_minutes: number | null;
  readiness_status: ReadinessStatus | null;
  planning_status: PlanningStatus | null;
  locked_window: Window | null;
  lat: number | null;
  lng: number | null;
  /** False means a district-centre pin, ~1-2km out. Say so rather than implying a doorstep. */
  precise_location: boolean;
  arrival: string;
  departure: string;
  drive_minutes_from_prev: number;
  distance_km_from_prev: number;
}

export type ActivePlan = PlanVersion & {
  stops: PlanStop[];
  depot: { lat: number; lng: number; address: string };
};

export interface AcceptResponse {
  offer: Offer;
  confirmed: boolean;
  /** Declining runs the agent in the same call: the time is excluded, the customer's dates are
   *  re-solved around it, and this is what came back. Null when nothing else can be fitted. */
  next_offer?: Offer | null;
  run?: AgentRun | null;
  evaluations?: Evaluation[];
  idempotent?: boolean;
  message: string | null;
  delivery_date: string | null;
  plan_version: number | null;
  /** Set when accepting moved the customer off a day they already held. */
  vacated_date?: string | null;
  vacated_plan_version?: number | null;
}

export interface Replacement {
  order_id: string;
  customer_name: string;
  currently_scheduled: string | null;
  score: number;
  window: Window;
}

export interface ReadinessResponse {
  order_id: string;
  readiness_status: ReadinessStatus;
  freed_date: string | null;
  plan_version: number | null;
  error: string | null;
  replacements: Replacement[];
}

export interface AgentAction {
  step: number;
  tool: string;
  ok: boolean;
  /** What the tool was called with, and what it returned. Both sanitised server-side. */
  arguments: Record<string, unknown>;
  data: Record<string, unknown>;
  summary: string;
  reason: string;
  error: string | null;
  timestamp: string;
}

export interface AgentRun {
  id: string;
  event_id: string;
  event_type: string | null;
  order_id: string | null;
  status: "running" | "completed" | "failed" | "step_limit_reached";
  final_summary: string;
  /** Which provider actually chose the actions -- "LLMDecisionAgent" or "RuleDecisionAgent". */
  decider: string;
  model_id: string | null;
  /** The exception that forced a fallback, kept so a credentials problem is distinguishable. */
  decider_error: string | null;
  /** Who read the customer's sentence -- a different decision, and often a different provider,
   *  from the one that chose the actions. Both are shown rather than one standing in for the
   *  other. */
  reader: string | null;
  reader_model_id: string | null;
  reader_error: string | null;
  started_at: string;
  completed_at: string | null;
  actions: AgentAction[];
}

/** One persisted message. `run_id` is the authoritative link to the trace behind it -- null for
 *  the customer's own words, which did not come from a tool call. */
export interface ChatMessage {
  id: string;
  direction: "inbound" | "outbound";
  body: string;
  created_at: string;
  run_id: string | null;
  offer_id: string | null;
}

/** The whole conversation as persisted, plus whatever the last message produced.
 *
 *  Returned by both sending a message and reloading, and identical either way -- which is what
 *  makes a browser refresh show exactly what was on screen before it. */
export interface ChatTurn {
  order_id: string;
  intent: string;
  duplicate: boolean;
  planning_status: string | null;
  confirmed: boolean;
  delivery_date: string | null;
  inbound_message_id: string | null;
  messages: ChatMessage[];
  /** Keyed by id, containing only the runs this thread's messages actually reference. */
  runs: Record<string, AgentRun>;
  offers: Record<string, Offer>;
  open_offer_id: string | null;
  /** The last run that actually decided something -- not necessarily the most recent run. A
   *  clarification question must not blank the panel explaining a confirmed booking. */
  decision: Decision | null;
  decision_run_id: string | null;
  /** The run this particular turn produced, or null when nothing ran. */
  run: AgentRun | null;
  error: string | null;
}

/** One link in "what changed" -- rejected, removed, re-solved, found. */
export interface DecisionStep {
  text: string;
  tone: "neutral" | "removed" | "solved" | "found";
}

/** One option, with the consequences of choosing it. */
export interface DecisionCandidate {
  label: string;
  /** "customer" matches what they asked for; "route" is easiest on the operation. */
  kind: "customer" | "route";
  badge: string;
  explanation: string;
  added_drive_minutes: number | null;
  added_distance_km: number | null;
  finishes_later_minutes: number | null;
  idle_minutes: number | null;
  overtime_minutes: number | null;
  promises_moved: number;
  opens_new_day: boolean;
  feasible: boolean;
  /** Where it lands in the solved day, in plain words. */
  insertion: string | null;
  stops_before: number | null;
  position: number | null;
  chosen: boolean;
  /** Whether this window is actually on the table. A candidate can be worth comparing and still
   *  not be offered -- when the requested time works and is not materially worse, we honour it. */
  offered: boolean;
  date: string;
  window: string;
  /** 24-hour start, for matching against the live offer without re-parsing prose. */
  start: string;
}

/** The agent's decision, in the shape a judge can read in five seconds.
 *
 *  Derived from persisted tool results. Not chain-of-thought, and structurally cannot be. */
export interface Decision {
  /** Changes with the event: "Why these times?", "Why the offer changed", "Why this time?",
   *  "What the agent changed". One panel answering every question answers none of them. */
  heading: string;
  asked: string;
  what_changed: string;
  steps: DecisionStep[];
  /** What the search looked at, in counts read straight off the tool result: stops compared,
   *  anchors found, positions tested, choices thrown away and why. A judge can check every
   *  one of these against the routes. */
  evidence: DecisionStep[];
  candidates: DecisionCandidate[];
  decision: string;
  outcome: string[];
  /** True, and collapsed. Nobody opens a panel to be told what a working day is. */
  planning_rules: string[];
  meaningful: boolean;
}

export interface Metrics {
  horizon: { first: string; last: string };
  scheduled_stops: number;
  round_trip_drive_minutes: number;
  plan_versions: number;
  confirmed_appointments_moved: number;
  customers_contacted: number;
  messages_sent: number;
  coordinator_interventions: number;
  delayed_orders: number;
  agent_runs: number;
}

export interface CoordinatorException {
  id: string;
  order_id: string | null;
  delivery_date: string | null;
  kind: string;
  message: string;
  created_at: string;
}

export interface MorningRunResponse {
  date: string;
  dispatched: number;
  reminders: number;
  plan_version: number | null;
  error: string | null;
}

export interface RoutePlanResponse {
  stops: Array<{
    sequence_index: number;
    job_id: string;
    customer_name: string;
    address: string;
    postal_code: string | null;
    job_type: string;
    lat: number;
    lng: number;
    arrival: string;
    departure: string;
    distance_from_prev_km: number;
    drive_minutes_from_prev: number;
  }>;
  total_drive_minutes: number;
  return_drive_minutes: number;
  round_trip_drive_minutes: number;
  total_distance_km: number;
  plan_version: number | null;
  error: string | null;
}

// -- Endpoints ----------------------------------------------------------------

export const dispatch = {
  bootstrap: () => api.get<Bootstrap>("/api/bootstrap"),
  horizon: () => api.get<Horizon>("/api/horizon"),
  orders: (planningStatus?: PlanningStatus) =>
    api.get<Order[]>(`/api/orders${planningStatus ? `?planning_status=${planningStatus}` : ""}`),
  metrics: () => api.get<Metrics>("/api/metrics"),
  exceptions: () => api.get<CoordinatorException[]>("/api/exceptions"),
  agentRuns: (limit = 20) => api.get<AgentRun[]>(`/api/agent-runs?limit=${limit}`),

  createOrder: (payload: unknown) => api.post<{ id: string; planning_status: string }>("/api/orders", payload),
  planOptions: (orderId: string) => api.post<PlanOptions>(`/api/orders/${orderId}/plan-options`),
  /** The single planning call: one agent run returning the offer AND the evaluations behind it,
   *  so what the customer is shown is by construction what the log records. */
  planAgentic: (orderId: string) => api.post<PlanOptions>(`/api/orders/${orderId}/plan-agentic`),
  respond: (offerId: string, accepted: boolean, slotId?: string) =>
    api.post<AcceptResponse>(`/api/offers/${offerId}/respond`, { accepted, slot_id: slotId ?? null }),

  /** One customer message. THE conversational call: one message, one agent run, one offer round. */
  sendMessage: (orderId: string, body: string) =>
    api.post<ChatTurn>(`/api/orders/${orderId}/messages`, { body }),
  /** The persisted thread, for a client that has just reloaded. */
  conversation: (orderId: string) => api.get<ChatTurn>(`/api/orders/${orderId}/messages`),

  activePlan: (date: string) => api.get<ActivePlan>(`/api/plans/${date}`),
  planVersions: (date: string) => api.get<PlanVersion[]>(`/api/plans/${date}/versions`),
  routePlan: (date: string) => api.post<RoutePlanResponse>("/api/route-plan", { date }),
  /** Send the finished route to the day's driver. A coordinator's action -- the customer-facing
   *  agent has no tool for this, by construction rather than by rule. */
  sendToDriver: (date: string) => api.post<DriverDispatch>(`/api/plans/${date}/dispatch`),

  setReadiness: (orderId: string, readiness: ReadinessStatus) =>
    api.post<ReadinessResponse>(`/api/orders/${orderId}/readiness`, { readiness_status: readiness }),
  morningRun: (date: string) => api.post<MorningRunResponse>("/api/events/morning-run", { date }),

  /** Put a freed slot to a customer who opted into an earlier delivery. An offer, not a move --
   *  they accept through the normal respond() path. */
  offerFreedSlot: (orderId: string, freedDate: string, window?: Window) =>
    api.post<{ offer: Offer; message: string; evaluation: Evaluation; error: string | null }>(
      "/api/recovery/offer",
      {
        order_id: orderId,
        freed_date: freedDate,
        window_start: window?.start ?? null,
        window_end: window?.end ?? null,
      },
    ),
};
