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

export interface Evaluation {
  availability_option_id: string;
  date: string;
  window: Window;
  feasible: boolean;
  infeasible_reason: string | null;
  total_score: number | null;
  breakdown: ScoreBreakdown;
  baseline_drive_minutes: number;
  proposed_drive_minutes: number;
}

export interface OfferSlot {
  id: string;
  /** The customer's original request this slot came from -- lets the UI show its evaluation. */
  availability_option_id: string;
  date: string;
  window: Window;
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
  message?: string;
  evaluations: Evaluation[];
  error: string | null;
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
  generated_at: string;
}

export interface PlanStop {
  sequence_index: number;
  job_id: string;
  customer_name: string;
  arrival: string;
  departure: string;
  drive_minutes_from_prev: number;
}

export type ActivePlan = PlanVersion & { stops: PlanStop[] };

export interface AcceptResponse {
  offer: Offer;
  confirmed: boolean;
  idempotent?: boolean;
  message: string | null;
  delivery_date: string | null;
  plan_version: number | null;
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
  summary: string;
  reason: string;
  error: string | null;
}

export interface AgentRun {
  id: string;
  event_id: string;
  event_type: string | null;
  order_id: string | null;
  status: "running" | "completed" | "failed" | "step_limit_reached";
  final_summary: string;
  started_at: string;
  completed_at: string | null;
  actions: AgentAction[];
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
  horizon: () => api.get<Horizon>("/api/horizon"),
  orders: (planningStatus?: PlanningStatus) =>
    api.get<Order[]>(`/api/orders${planningStatus ? `?planning_status=${planningStatus}` : ""}`),
  metrics: () => api.get<Metrics>("/api/metrics"),
  exceptions: () => api.get<CoordinatorException[]>("/api/exceptions"),
  agentRuns: (limit = 20) => api.get<AgentRun[]>(`/api/agent-runs?limit=${limit}`),

  createOrder: (payload: unknown) => api.post<{ id: string; planning_status: string }>("/api/orders", payload),
  planOptions: (orderId: string) => api.post<PlanOptions>(`/api/orders/${orderId}/plan-options`),
  planAgentic: (orderId: string) =>
    api.post<{ run: AgentRun; offer: Offer | null }>(`/api/orders/${orderId}/plan-agentic`),
  respond: (offerId: string, accepted: boolean, slotId?: string) =>
    api.post<AcceptResponse>(`/api/offers/${offerId}/respond`, { accepted, slot_id: slotId ?? null }),

  activePlan: (date: string) => api.get<ActivePlan>(`/api/plans/${date}`),
  planVersions: (date: string) => api.get<PlanVersion[]>(`/api/plans/${date}/versions`),
  routePlan: (date: string) => api.post<RoutePlanResponse>("/api/route-plan", { date }),

  setReadiness: (orderId: string, readiness: ReadinessStatus) =>
    api.post<ReadinessResponse>(`/api/orders/${orderId}/readiness`, { readiness_status: readiness }),
  morningRun: (date: string) => api.post<MorningRunResponse>("/api/events/morning-run", { date }),
};
