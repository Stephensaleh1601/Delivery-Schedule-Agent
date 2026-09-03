"""FastAPI replacement for the Streamlit dashboard: a WhatsApp-style client-facing chat
(static/client.html) and a back-office dispatch dashboard (static/admin.html), talking to the
JSON API below over plain fetch() -- no build step, no frontend framework.

Run with: uvicorn dispatch_agent.webapp.main:app --reload
"""
from __future__ import annotations

from datetime import date as Date, time as Time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import COMPANY_DEPOT, COMPANY_DEPOT_ADDRESS
from dispatch_agent.agents.scheduling_agent import handle_planning_event
from dispatch_agent.models import (
    DaySequence,
    JobRecord,
    JobStatus,
    AgentRunStatus,
    Notification,
    PlanningEvent,
    PlanningEventType,
    PlanningStatus,
    ReadinessStatus,
    TimeWindow,
)
from dispatch_agent.planning import offer_service, plan_service, recovery_service, tools
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.solver import UnsolvableDayError, sequence_day
from dispatch_agent.webapp import chat as chat_engine
from dispatch_agent.webapp.jobs_service import (
    JobSubmission,
    JobSubmissionError,
    OrderSubmission,
    create_job_from_submission,
    create_order,
    update_job_from_submission,
)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Dispatch Sequencing")
init_db()


class RoutePlanRequest(BaseModel):
    date: Date


class ChatEvent(BaseModel):
    type: str
    value: str | None = None
    form: str | None = None
    data: dict[str, Any] | None = None
    state: dict[str, Any] | None = None


def _sequence_has_stops(sequence: DaySequence | None) -> bool:
    return sequence is not None and bool(sequence.stops)


def _job_to_dict(job: JobRecord) -> dict:
    return {
        "id": job.id,
        "customer_name": job.customer_name,
        "phone": job.phone,
        "address": job.address.raw_text,
        "postal_code": job.address.postal_code,
        "job_type": job.job_type.value,
        # None until a slot is agreed. Callers must handle that -- an order in the planning pool
        # genuinely has no date yet.
        "delivery_date": job.delivery_date.isoformat() if job.delivery_date else None,
        "availability": [
            {"start": w.start.strftime("%H:%M"), "end": w.end.strftime("%H:%M")} for w in job.availability
        ],
        "availability_options": [
            {
                "id": o.id,
                "date": o.date.isoformat(),
                "start": o.window.start.strftime("%H:%M"),
                "end": o.window.end.strftime("%H:%M"),
                "preference_rank": o.preference_rank,
            }
            for o in job.availability_options
        ],
        "locked_window": (
            {
                "start": job.locked_window.start.strftime("%H:%M"),
                "end": job.locked_window.end.strftime("%H:%M"),
            }
            if job.locked_window
            else None
        ),
        "duration_minutes": job.duration_minutes,
        "planning_status": job.planning_status.value,
        "readiness_status": job.readiness_status.value,
        "can_deliver_early": job.can_deliver_early,
        "status": job.status.value,
        "notes": job.notes,
    }


# -- Client-facing intake (front face) ----------------------------------------


@app.post("/api/jobs")
def submit_job(payload: JobSubmission) -> dict:
    try:
        job = create_job_from_submission(payload, raw_message="[submitted via client booking form]")
    except JobSubmissionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": job.id, "status": "ok"}


@app.post("/api/chat")
def chat_turn(event: ChatEvent) -> dict:
    """The WhatsApp-style front face's only endpoint -- see dispatch_agent/webapp/chat.py for
    the actual conversation state machine (booking form + reschedule negotiation)."""
    return chat_engine.handle_event(event.model_dump())


# -- Back office (back face) --------------------------------------------------


@app.get("/api/jobs")
def list_jobs(date: Date | None = None) -> list[dict]:
    repo = JobsRepository()
    jobs = repo.jobs_for_date(date) if date else repo.all_jobs()
    return [_job_to_dict(j) for j in jobs]


@app.get("/api/dates")
def list_dates() -> list[str]:
    return [d.isoformat() for d in JobsRepository().pending_dates()]


@app.put("/api/jobs/{job_id}")
def edit_job(job_id: str, payload: JobSubmission) -> dict:
    repo = JobsRepository()
    existing = repo.get_job(job_id)
    if existing is None:
        raise HTTPException(404, "Job not found")
    previous_date = existing.delivery_date

    try:
        job = update_job_from_submission(job_id, payload)
    except JobSubmissionError as exc:
        raise HTTPException(400, str(exc)) from exc

    stale_dates = sorted(
        {
            d
            for d in {previous_date, job.delivery_date}
            if d is not None and _sequence_has_stops(repo.get_sequence(d))
        }
    )
    if stale_dates:
        dates_text = " and ".join(d.isoformat() for d in stale_dates)
        repo.add_notification(
            Notification(
                message=f"{job.customer_name}'s order was edited. The route plan for {dates_text} "
                "was already generated and is now out of date -- regenerate it.",
                dates=stale_dates,
            )
        )
    return {"id": job.id, "status": "ok"}


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str) -> dict:
    repo = JobsRepository()
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    was_sequenced = job.delivery_date is not None and _sequence_has_stops(
        repo.get_sequence(job.delivery_date)
    )
    repo.delete_job(job_id)

    if was_sequenced:
        repo.add_notification(
            Notification(
                message=f"{job.customer_name}'s order was deleted. The route plan for "
                f"{job.delivery_date.isoformat()} was already generated and is now out of date -- "
                "regenerate it.",
                dates=[job.delivery_date],
            )
        )
    return {"status": "ok"}


@app.get("/api/notifications")
def list_notifications() -> list[dict]:
    """Unread notifications for the back office -- e.g. a chat reschedule that made an
    already-generated route plan stale (see dispatch_agent/webapp/chat.py)."""
    return [
        {
            "id": n.id,
            "message": n.message,
            "dates": [d.isoformat() for d in n.dates],
            "created_at": n.created_at.isoformat(),
        }
        for n in JobsRepository().unread_notifications()
    ]


@app.post("/api/notifications/{notification_id}/dismiss")
def dismiss_notification(notification_id: str) -> dict:
    JobsRepository().mark_notification_read(notification_id)
    return {"status": "ok"}


@app.get("/api/config")
def frontend_config() -> dict:
    """Everything the admin page's map needs but shouldn't hardcode: the Maps JS API key
    (exposed to the browser by design -- Google's own security model for this key is HTTP
    referrer restriction in Cloud Console, not secrecy) and the depot location/address."""
    return {
        "google_maps_api_key": settings.google_maps_api_key,
        "depot": {"lat": COMPANY_DEPOT.lat, "lng": COMPANY_DEPOT.lng, "address": COMPANY_DEPOT_ADDRESS},
    }


@app.post("/api/route-plan")
def generate_route_plan(payload: RoutePlanRequest) -> dict:
    """Solve and publish a date's route.

    Goes through plan_service so the result is versioned and every confirmed appointment is
    verified before anything is written -- previously this called the solver directly and
    overwrote day_sequences, bypassing both.
    """
    repo = JobsRepository()
    jobs = plan_service.routable_jobs(repo, payload.date)
    if not jobs:
        return {
            "stops": [], "total_drive_minutes": 0, "return_drive_minutes": 0,
            "total_distance_km": 0, "plan_version": None, "error": None,
        }

    routing_client = RoutingClient()
    try:
        plan = plan_service.replan_day(
            repo, payload.date, reason="generated from the dashboard", routing_client=routing_client
        )
    except (UnsolvableDayError, ValueError) as exc:
        return {
            "stops": [], "total_drive_minutes": 0, "return_drive_minutes": 0,
            "total_distance_km": 0, "plan_version": None, "error": str(exc),
        }
    sequence = plan.sequence

    jobs_by_id = {j.id: j for j in jobs}

    stops = []
    for stop in sequence.stops:
        job = jobs_by_id[stop.job_id]
        # Distance now comes from the published plan. This used to run its own leg_distances call
        # that omitted the return leg, so it disagreed with the plan it had just published.
        stops.append(
            {
                **_stop_to_dict(stop, job),
                "phone": job.phone,
                "distance_from_prev_km": stop.distance_km_from_prev,
            }
        )
        # Advance ONLY a confirmed appointment to sequenced. The old code set this on every job
        # unconditionally, which under the planning lifecycle would overwrite CONFIRMED (and
        # OFFERED, and PENDING_*) every time a coordinator regenerated a plan -- silently
        # discarding the state that says a customer was promised something.
        if job.planning_status is PlanningStatus.CONFIRMED:
            job.set_planning_status(PlanningStatus.SEQUENCED)
            repo.save_job(job)

    # The plan (and the day_sequences cache) was already persisted by plan_service.
    return {
        "stops": stops,
        "total_drive_minutes": sequence.total_drive_minutes,
        "return_drive_minutes": sequence.return_drive_minutes,
        "round_trip_drive_minutes": sequence.round_trip_drive_minutes,
        "total_distance_km": sequence.total_distance_km,
        "round_trip_distance_km": sequence.round_trip_distance_km,
        "distance_recorded": sequence.distance_recorded,
        "plan_version": plan.version,
        "plan_id": plan.id,
        "error": None,
    }


# -- Multi-day planning --------------------------------------------------------


class OfferResponse(BaseModel):
    accepted: bool
    slot_id: str | None = None
    # Optional client-supplied key. A repeated submission with the same key returns the stored
    # outcome instead of doing the work twice.
    event_id: str | None = None


def _offer_to_dict(offer) -> dict:
    return {
        "id": offer.id,
        "order_id": offer.order_id,
        "round_number": offer.round_number,
        "status": offer.status.value,
        "accepted_slot_id": offer.accepted_slot_id,
        "options": [
            {
                "id": slot.id,
                # Lets a client line an offered slot up with its evaluation, so the UI can show
                # the reasoning behind the offer rather than just the offer.
                "availability_option_id": slot.availability_option_id,
                "date": slot.date.isoformat(),
                "window": {
                    "start": slot.window.start.strftime("%H:%M"),
                    "end": slot.window.end.strftime("%H:%M"),
                },
                "label": f"{offer_service.format_date(slot.date)}, {offer_service.format_window(slot.window)}",
                # Why this time, read off the solved route. Safe for the customer-facing bubble:
                # route_facts.customer_reason names nobody else and quotes no score.
                "reason": slot.reason,
            }
            for slot in offer.options
        ],
    }


def _evaluation_to_dict(evaluation) -> dict:
    """The coordinator-facing view of a candidate. The breakdown is included deliberately: a
    single opaque score is not something anyone can sanity-check or argue with."""
    return {
        "availability_option_id": evaluation.availability_option_id,
        "date": evaluation.date.isoformat(),
        "window": {
            "start": evaluation.window.start.strftime("%H:%M"),
            "end": evaluation.window.end.strftime("%H:%M"),
        },
        # What we would actually put to the customer: the narrow window derived from the arrival the
        # solver chose. `window` above stays what they ASKED for -- the two are different now, and a
        # surface showing the wrong one either over-promises or under-sells the negotiation.
        "promise_window": {
            "start": evaluation.promise_window.start.strftime("%H:%M"),
            "end": evaluation.promise_window.end.strftime("%H:%M"),
        }
        if evaluation.promise_window
        else None,
        "service_window": {
            "start": evaluation.service_window.start.strftime("%H:%M"),
            "end": evaluation.service_window.end.strftime("%H:%M"),
        }
        if evaluation.service_window
        else None,
        "feasible": evaluation.feasible,
        "infeasible_reason": evaluation.infeasible_reason,
        "total_score": evaluation.total_score if evaluation.feasible else None,
        "breakdown": {
            "incremental_drive_minutes": evaluation.incremental_drive_minutes,
            "day_opening_penalty_minutes": evaluation.day_opening_penalty_minutes,
            "preference_penalty_minutes": evaluation.preference_penalty_minutes,
            "overtime_penalty_minutes": evaluation.overtime_penalty_minutes,
        },
        "baseline_drive_minutes": evaluation.baseline_drive_minutes,
        "proposed_drive_minutes": evaluation.proposed_drive_minutes,
        # The same figures decomposed for display. Route efficiency is the objective; preference
        # stays inside `breakdown` because promoting it here would imply it is a routing fact.
        "route_impact": {
            "drive_minutes": {
                "before": evaluation.baseline_drive_minutes,
                "after": evaluation.proposed_drive_minutes,
            },
            "distance_km": {
                "before": evaluation.baseline_distance_km,
                "after": evaluation.proposed_distance_km,
            },
            "stops": {
                "before": evaluation.baseline_stop_count,
                "after": evaluation.proposed_stop_count,
            },
            # Real minutes worked past the soft day end, unlike the penalties above it.
            "overtime_minutes": evaluation.overtime_penalty_minutes,
            "finishes_at": {
                # None when the day had no stops -- "this day did not exist yet" is the honest
                # rendering, not 00:00.
                "before": _hhmm(evaluation.baseline_completion_minutes)
                if evaluation.baseline_stop_count
                else None,
                "after": _hhmm(evaluation.proposed_completion_minutes),
            },
            "opens_empty_day": evaluation.opens_empty_day,
            "region": evaluation.region,
            "position": evaluation.route_position,
            "stop_count": evaluation.route_stop_count,
            # Read off the solved sequence, not written by a model. The customer one names nobody
            # else; the coordinator one is for the operations panel.
            "customer_reason": evaluation.customer_reason,
            "coordinator_reason": evaluation.coordinator_reason,
            "empty_day_overhead_minutes": evaluation.day_opening_penalty_minutes,
            "preference_rank": evaluation.preference_rank,
        },
    }


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@app.get("/api/bootstrap")
def bootstrap() -> dict:
    """Everything a client needs before it can render anything, in one request.

    Includes the operating constants the UI would otherwise hardcode -- the working day, the soft
    finish time, and the scoring weights -- so an interface explaining a route impact quotes the
    same numbers the solver used rather than a copy that can drift.
    """
    first, last = PlanningClock.horizon()
    return {
        "horizon": {
            "today": PlanningClock.today().isoformat(),
            "first": first.isoformat(),
            "last": last.isoformat(),
            "dates": [d.isoformat() for d in PlanningClock.horizon_dates()],
        },
        "map": {
            "google_maps_api_key": settings.google_maps_api_key,
            "depot": {
                "lat": COMPANY_DEPOT.lat,
                "lng": COMPANY_DEPOT.lng,
                "address": COMPANY_DEPOT_ADDRESS,
            },
        },
        "operating": {
            "work_day_start": settings.work_day_start.strftime("%H:%M"),
            "work_day_end": settings.work_day_end.strftime("%H:%M"),
            "soft_day_end": settings.soft_day_end.strftime("%H:%M"),
            "day_opening_penalty_minutes": settings.day_opening_penalty_minutes,
            "preference_penalty_per_rank": settings.preference_penalty_per_rank,
            "routing_provider": settings.routing_provider,
        },
    }


@app.get("/api/horizon")
def planning_horizon() -> dict:
    """The bookable window. The browser needs this for its date inputs' min/max, and the server
    re-validates anyway -- a date range enforced only in HTML is not enforced."""
    first, last = PlanningClock.horizon()
    return {
        "today": PlanningClock.today().isoformat(),
        "first": first.isoformat(),
        "last": last.isoformat(),
        "dates": [d.isoformat() for d in PlanningClock.horizon_dates()],
    }


@app.post("/api/orders")
def create_order_endpoint(payload: OrderSubmission) -> dict:
    """Book an order with 2-3 acceptable windows and no agreed date."""
    try:
        job = create_order(payload, raw_message="[submitted via booking form]")
    except JobSubmissionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": job.id, "planning_status": job.planning_status.value, "status": "ok"}


@app.get("/api/orders")
def list_orders(planning_status: str | None = None) -> list[dict]:
    repo = JobsRepository()
    if planning_status:
        try:
            jobs = repo.jobs_by_planning_status(PlanningStatus(planning_status))
        except ValueError as exc:
            raise HTTPException(400, f"unknown planning status {planning_status!r}") from exc
    else:
        jobs = repo.all_jobs()
    return [_job_to_dict(j) for j in jobs]


@app.post("/api/orders/{order_id}/plan-options")
def plan_options(order_id: str) -> dict:
    """Evaluate the customer's windows and offer the best feasible ones.

    Returns the full evaluation alongside the offer so the ops UI can show *why* a slot was
    chosen, not merely which one.
    """
    repo = JobsRepository()
    order = repo.get_job(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")

    evaluations = CandidateService(repo=repo).evaluate_all(order)

    open_offer = repo.open_offer_for_order(order_id)
    if open_offer is not None:
        # Slots are already with this customer. Show the reasoning against the offer that exists
        # rather than minting a second one -- create_offer excludes already-offered windows, so a
        # competing offer would hold DIFFERENT slots from the ones they were sent.
        return {
            "offer": _offer_to_dict(open_offer),
            "message": None,
            "evaluations": [_evaluation_to_dict(e) for e in evaluations],
            "reused": True,
            "error": None,
        }

    try:
        offer = offer_service.create_offer(repo, order, evaluations)
    except offer_service.OfferError as exc:
        # Only a genuinely unservable set of windows is a coordinator's problem. The other kinds
        # are our own bookkeeping and must not be reported as "none of the windows can be fitted".
        if exc.kind == "no_feasible_slot":
            plan_service.raise_coordinator_exception(
                repo, str(exc), kind=exc.kind, order_id=order_id
            )
        return {
            "offer": None,
            "evaluations": [_evaluation_to_dict(e) for e in evaluations],
            "reused": False,
            "error": str(exc),
        }

    message = offer_service.offer_message(offer)
    offer_service.record_message(repo, order_id, message)
    return {
        "offer": _offer_to_dict(offer),
        "message": message,
        "evaluations": [_evaluation_to_dict(e) for e in evaluations],
        "reused": False,
        "error": None,
    }


@app.get("/api/offers/{offer_id}")
def get_offer(offer_id: str) -> dict:
    offer = JobsRepository().get_offer(offer_id)
    if offer is None:
        raise HTTPException(404, "Offer not found")
    return _offer_to_dict(offer)


@app.post("/api/offers/{offer_id}/respond")
def respond_to_offer(offer_id: str, payload: OfferResponse) -> dict:
    """Accept or reject an offered slot. Safe to replay -- see offer_service.accept_offer."""
    repo = JobsRepository()
    if not payload.accepted:
        declined = repo.get_offer(offer_id)
        if declined is None:
            raise HTTPException(404, "Offer not found")

        # Declining runs the agent, exactly as a new order does. The rejection is not a dead end:
        # the tools exclude the time that was turned down, re-solve the customer's dates around the
        # hole, and come back with a different window -- so this returns the next offer, the run
        # that produced it, and the evaluations behind it, all from the one call.
        ctx = tools.ToolContext(repo=repo)
        run = handle_planning_event(
            PlanningEvent(
                event_type=PlanningEventType.CUSTOMER_REJECTED_OFFER,
                order_id=declined.order_id,
                payload={"offer_id": offer_id, "slot_id": payload.slot_id},
            ),
            repo=repo,
            ctx=ctx,
        )
        next_offer = repo.get_offer(ctx.offer_id) if ctx.offer_id else None
        return {
            "offer": _offer_to_dict(repo.get_offer(offer_id)),
            "confirmed": False,
            "next_offer": _offer_to_dict(next_offer) if next_offer else None,
            "message": ctx.scratch.get("offer_message"),
            "run": _run_to_dict(run),
            "evaluations": [_evaluation_to_dict(e) for e in ctx.evaluations],
        }

    if not payload.slot_id:
        raise HTTPException(400, "slot_id is required when accepting")
    try:
        outcome = offer_service.accept_offer(repo, offer_id, payload.slot_id)
    except offer_service.OfferError as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "offer": _offer_to_dict(outcome.offer),
        "confirmed": True,
        "idempotent": outcome.idempotent,
        "message": outcome.message,
        "delivery_date": outcome.job.delivery_date.isoformat() if outcome.job.delivery_date else None,
        "plan_version": outcome.plan.version if outcome.plan else None,
        # Present when the customer moved off a day they already held.
        "vacated_date": outcome.vacated_date.isoformat() if outcome.vacated_date else None,
        "vacated_plan_version": outcome.vacated_plan.version if outcome.vacated_plan else None,
    }


def _plan_to_dict(plan) -> dict:
    return {
        "id": plan.id,
        "delivery_date": plan.delivery_date.isoformat(),
        "version": plan.version,
        "status": plan.status.value,
        "reason_created": plan.reason_created,
        "parent_plan_id": plan.parent_plan_id,
        "stop_count": len(plan.sequence.stops),
        "total_drive_minutes": plan.sequence.total_drive_minutes,
        "return_drive_minutes": plan.sequence.return_drive_minutes,
        "round_trip_drive_minutes": plan.sequence.round_trip_drive_minutes,
        "total_distance_km": plan.sequence.total_distance_km,
        "return_distance_km": plan.sequence.return_distance_km,
        "round_trip_distance_km": plan.sequence.round_trip_distance_km,
        # False for plans published before distance was recorded. Render "not recorded" rather
        # than a 0 km bar beside a real one.
        "distance_recorded": plan.sequence.distance_recorded,
        "finishes_at": _hhmm(plan.sequence.completion_minutes) if plan.sequence.stops else None,
        "generated_at": plan.generated_at.isoformat(),
    }


@app.get("/api/plans/{plan_date}")
def get_plan(plan_date: Date) -> dict:
    plan = JobsRepository().active_plan(plan_date)
    if plan is None:
        raise HTTPException(404, "No plan for that date")
    repo = JobsRepository()
    # One lookup per stop, not two. This used to call get_job twice for every row.
    jobs_by_id = {job.id: job for job in repo.jobs_for_date(plan_date)}
    return {
        **_plan_to_dict(plan),
        "depot": {"lat": COMPANY_DEPOT.lat, "lng": COMPANY_DEPOT.lng, "address": COMPANY_DEPOT_ADDRESS},
        "stops": [_stop_to_dict(stop, jobs_by_id.get(stop.job_id)) for stop in plan.sequence.stops],
    }


def _stop_to_dict(stop, job: JobRecord | None) -> dict:
    """A stop, with everything a map needs to draw it."""
    coords = job.address.coordinates if job and job.address.coordinates else None
    return {
        "sequence_index": stop.sequence_index + 1,
        "job_id": stop.job_id,
        "customer_name": job.customer_name if job else "Unknown",
        "address": job.address.formatted_address or job.address.raw_text if job else None,
        "postal_code": job.address.postal_code if job else None,
        "job_type": job.job_type.value if job else None,
        "duration_minutes": job.duration_minutes if job else None,
        "readiness_status": job.readiness_status.value if job else None,
        "planning_status": job.planning_status.value if job else None,
        "locked_window": (
            {"start": job.locked_window.start.strftime("%H:%M"),
             "end": job.locked_window.end.strftime("%H:%M")}
            if job and job.locked_window
            else None
        ),
        "lat": coords.lat if coords else None,
        "lng": coords.lng if coords else None,
        # False means the pin is a district centre, ~1-2km out. The UI should say so.
        "precise_location": bool(job and job.address.precisely_located),
        "arrival": stop.arrival_window.start.strftime("%H:%M"),
        "departure": stop.arrival_window.end.strftime("%H:%M"),
        "drive_minutes_from_prev": stop.drive_minutes_from_prev,
        "distance_km_from_prev": stop.distance_km_from_prev,
    }


@app.get("/api/plans/{plan_date}/versions")
def list_plan_versions(plan_date: Date) -> list[dict]:
    """Every version of a day's plan, oldest first -- the v1-vs-v2 comparison."""
    return [_plan_to_dict(p) for p in JobsRepository().plan_versions(plan_date)]


def _run_to_dict(run) -> dict:
    """The activity feed. Every entry is a real persisted tool call and its outcome -- there is
    no decorative narration in here, which is the point."""
    return {
        "id": run.id,
        "event_id": run.event_id,
        "event_type": run.event_type.value if run.event_type else None,
        "order_id": run.order_id,
        "status": run.status.value,
        "final_summary": run.final_summary,
        # Which provider actually chose these actions. Stated rather than implied, so the inspector
        # cannot present a rule-driven run as a model-driven one.
        "decider": run.decider,
        "model_id": run.model_id,
        "decider_error": run.decider_error,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "actions": [
            {
                "step": a.step,
                "tool": a.tool,
                "ok": a.ok,
                # Input and result, both already sanitised where the log was written.
                "arguments": a.arguments,
                "data": a.data,
                "summary": a.summary,
                "reason": a.reason_summary,
                "error": a.error,
                "timestamp": a.timestamp.isoformat(),
            }
            for a in run.actions
        ],
    }


@app.get("/api/agent-runs")
def list_agent_runs(limit: int = 20) -> list[dict]:
    return [_run_to_dict(r) for r in JobsRepository().agent_runs(limit=limit)]


@app.post("/api/orders/{order_id}/plan-agentic")
def plan_agentically(order_id: str) -> dict:
    """Plan a new order. THE conversation's single planning call.

    Returns the run, the offer and the evaluations from one agent run, so the slots the customer
    is shown are by construction the slots in the log. Calling this and /plan-options in sequence
    used to open two negotiations with different slots in each.
    """
    repo = JobsRepository()
    order = repo.get_job(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")

    # A second press must not start a competing negotiation while the first is unanswered.
    open_offer = repo.open_offer_for_order(order_id)
    if open_offer is not None:
        return {
            "run": None,
            "offer": _offer_to_dict(open_offer),
            "message": None,
            "evaluations": [],
            "reused": True,
            "error": None,
        }

    ctx = tools.ToolContext(repo=repo)
    run = handle_planning_event(
        PlanningEvent(event_type=PlanningEventType.NEW_ORDER, order_id=order_id),
        repo=repo,
        ctx=ctx,
    )
    # ctx.offer_id is the authoritative link to the offer THIS run made. Picking the newest row
    # instead is how a caller ends up showing slots from a different offer than the log records.
    offer = repo.get_offer(ctx.offer_id) if ctx.offer_id else None
    return {
        "run": _run_to_dict(run),
        "offer": _offer_to_dict(offer) if offer else None,
        "message": ctx.scratch.get("offer_message"),
        "evaluations": [_evaluation_to_dict(e) for e in ctx.evaluations],
        "reused": False,
        "error": None if run.status is AgentRunStatus.COMPLETED else run.final_summary,
    }


class ReadinessUpdate(BaseModel):
    readiness_status: str


@app.post("/api/orders/{order_id}/readiness")
def update_readiness(order_id: str, payload: ReadinessUpdate) -> dict:
    """The mock ERP signal. Marking an order delayed takes it off the route and looks for a
    customer who would take the freed slot."""
    try:
        readiness = ReadinessStatus(payload.readiness_status)
    except ValueError as exc:
        raise HTTPException(400, f"unknown readiness status {payload.readiness_status!r}") from exc

    repo = JobsRepository()
    try:
        outcome = recovery_service.mark_readiness(repo, order_id, readiness)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    return {
        "order_id": outcome.job.id,
        "readiness_status": outcome.job.readiness_status.value,
        "freed_date": outcome.freed_date.isoformat() if outcome.freed_date else None,
        "plan_version": outcome.plan.version if outcome.plan else None,
        "error": outcome.error,
        "replacements": [
            {
                "order_id": c.job.id,
                "customer_name": c.job.customer_name,
                "currently_scheduled": c.job.delivery_date.isoformat() if c.job.delivery_date else None,
                "score": c.score,
                "window": {
                    "start": c.evaluation.window.start.strftime("%H:%M"),
                    "end": c.evaluation.window.end.strftime("%H:%M"),
                },
            }
            for c in outcome.replacements
        ],
    }


class RecoveryOfferRequest(BaseModel):
    order_id: str
    freed_date: Date
    window_start: str | None = None
    window_end: str | None = None


@app.post("/api/recovery/offer")
def offer_recovery_slot(payload: RecoveryOfferRequest) -> dict:
    """Put a freed slot to a customer who agreed to come forward.

    An OFFER, not a move. Nothing about their existing appointment changes until they accept it
    themselves through the normal /api/offers/{id}/respond path.
    """
    repo = JobsRepository()
    window = None
    if payload.window_start and payload.window_end:
        window = TimeWindow(
            start=Time.fromisoformat(payload.window_start),
            end=Time.fromisoformat(payload.window_end),
        )
    try:
        offer, message, evaluation = recovery_service.offer_freed_slot(
            repo, payload.order_id, payload.freed_date, window=window
        )
    except offer_service.OfferError as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "offer": _offer_to_dict(offer),
        "message": message,
        "evaluation": _evaluation_to_dict(evaluation),
        "error": None,
    }


@app.post("/api/events/morning-run")
def morning_run(payload: RoutePlanRequest | None = None) -> dict:
    """Finalise a day for dispatch: republish the route from what is confirmed and ready, mark
    those stops dispatched, and draft a reminder for each customer.

    A button rather than a scheduler on purpose -- it calls exactly the service a cron job would,
    so nothing about the flow is demo-only scaffolding.
    """
    repo = JobsRepository()
    target = payload.date if payload else PlanningClock.today()

    jobs = plan_service.routable_jobs(repo, target)
    if not jobs:
        return {"date": target.isoformat(), "dispatched": 0, "reminders": 0,
                "plan_version": None, "error": "nothing confirmed and ready for that date"}

    try:
        plan = plan_service.replan_day(repo, target, reason="morning run")
    except (UnsolvableDayError, ValueError) as exc:
        plan_service.raise_coordinator_exception(
            repo, f"Morning run could not publish {target}: {exc}",
            kind="morning_run_failed", delivery_date=target,
        )
        return {"date": target.isoformat(), "dispatched": 0, "reminders": 0,
                "plan_version": None, "error": str(exc)}

    reminders = 0
    jobs_by_id = {j.id: j for j in jobs}
    for stop in plan.sequence.stops:
        job = jobs_by_id.get(stop.job_id)
        if job is None:
            continue
        if job.planning_status in (PlanningStatus.CONFIRMED, PlanningStatus.SEQUENCED):
            job.set_planning_status(PlanningStatus.DISPATCHED)
            repo.save_job(job)
        offer_service.record_message(
            repo,
            job.id,
            f"Good morning! Your {job.job_type.value} delivery is today between "
            f"{offer_service.format_time(stop.arrival_window.start)} and "
            f"{offer_service.format_time(stop.arrival_window.end)}.",
        )
        reminders += 1

    return {
        "date": target.isoformat(),
        "dispatched": len(plan.sequence.stops),
        "reminders": reminders,
        "plan_version": plan.version,
        "error": None,
    }


@app.get("/api/metrics")
def metrics() -> dict:
    """Impact figures, computed from what is actually stored.

    Nothing here is estimated or assumed. "Confirmed appointments moved" is a real count taken by
    comparing every published stop against the window that customer was promised, which is the
    number the whole design exists to keep at zero.
    """
    repo = JobsRepository()
    dates = PlanningClock.horizon_dates()

    appointments_moved = 0
    plan_versions = 0
    scheduled_stops = 0
    drive_minutes = 0
    for day in dates:
        versions = repo.plan_versions(day)
        plan_versions += len(versions)
        active = repo.active_plan(day)
        if active is None:
            continue
        scheduled_stops += len(active.sequence.stops)
        drive_minutes += active.sequence.round_trip_drive_minutes
        for stop in active.sequence.stops:
            job = repo.get_job(stop.job_id)
            if job is None or not job.is_locked:
                continue
            lock = job.locked_window
            if not (lock.start <= stop.arrival_window.start and stop.arrival_window.end <= lock.end):
                appointments_moved += 1

    outbound = [m for m in repo.messages() if m.direction.value == "outbound"]
    delayed = [
        j for j in repo.all_jobs() if j.readiness_status is ReadinessStatus.DELAYED
    ]

    return {
        "horizon": {"first": dates[0].isoformat(), "last": dates[-1].isoformat()},
        "scheduled_stops": scheduled_stops,
        "round_trip_drive_minutes": drive_minutes,
        "plan_versions": plan_versions,
        "confirmed_appointments_moved": appointments_moved,
        "customers_contacted": len({m.order_id for m in outbound if m.order_id}),
        "messages_sent": len(outbound),
        "coordinator_interventions": len(repo.open_exceptions()),
        "delayed_orders": len(delayed),
        "agent_runs": len(repo.agent_runs(limit=500)),
    }


@app.get("/api/exceptions")
def list_exceptions() -> list[dict]:
    return [
        {
            "id": e.id,
            "order_id": e.order_id,
            "delivery_date": e.delivery_date.isoformat() if e.delivery_date else None,
            "kind": e.kind,
            "message": e.message,
            "created_at": e.created_at.isoformat(),
        }
        for e in JobsRepository().open_exceptions()
    ]


# -- Static frontend -----------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def client_form() -> FileResponse:
    return FileResponse(STATIC_DIR / "client.html")


@app.get("/admin")
def admin_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")
