"""FastAPI replacement for the Streamlit dashboard: a WhatsApp-style client-facing chat
(static/client.html) and a back-office dispatch dashboard (static/admin.html), talking to the
JSON API below over plain fetch() -- no build step, no frontend framework.

Run with: uvicorn dispatch_agent.webapp.main:app --reload
"""
from __future__ import annotations

from datetime import date as Date
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
from dispatch_agent.models import (
    DaySequence,
    JobRecord,
    JobStatus,
    Notification,
    PlanningStatus,
)
from dispatch_agent.planning import offer_service, plan_service
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
    ordered_points = [COMPANY_DEPOT] + [jobs_by_id[s.job_id].address.coordinates for s in sequence.stops]
    legs = routing_client.leg_distances(ordered_points)  # one batched call, not one per stop

    stops = []
    total_km = 0.0
    for stop, leg in zip(sequence.stops, legs):
        job = jobs_by_id[stop.job_id]
        coords = job.address.coordinates
        total_km += leg["km"]
        stops.append(
            {
                "sequence_index": stop.sequence_index + 1,
                "job_id": job.id,
                "customer_name": job.customer_name,
                "phone": job.phone,
                "address": job.address.raw_text,
                "postal_code": job.address.postal_code,
                "job_type": job.job_type.value,
                "lat": coords.lat,
                "lng": coords.lng,
                "arrival": stop.arrival_window.start.strftime("%H:%M"),
                "departure": stop.arrival_window.end.strftime("%H:%M"),
                "distance_from_prev_km": leg["km"],
                "drive_minutes_from_prev": stop.drive_minutes_from_prev,
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
        "total_distance_km": round(total_km, 2),
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
                "date": slot.date.isoformat(),
                "window": {
                    "start": slot.window.start.strftime("%H:%M"),
                    "end": slot.window.end.strftime("%H:%M"),
                },
                "label": f"{offer_service.format_date(slot.date)}, {offer_service.format_window(slot.window)}",
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
    try:
        offer = offer_service.create_offer(repo, order, evaluations)
    except offer_service.OfferError as exc:
        plan_service.raise_coordinator_exception(
            repo, str(exc), kind="no_feasible_slot", order_id=order_id
        )
        return {
            "offer": None,
            "evaluations": [_evaluation_to_dict(e) for e in evaluations],
            "error": str(exc),
        }

    message = offer_service.offer_message(offer)
    offer_service.record_message(repo, order_id, message)
    return {
        "offer": _offer_to_dict(offer),
        "message": message,
        "evaluations": [_evaluation_to_dict(e) for e in evaluations],
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
        offer = offer_service.reject_offer(repo, offer_id)
        return {"offer": _offer_to_dict(offer), "confirmed": False, "message": None}

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
        "generated_at": plan.generated_at.isoformat(),
    }


@app.get("/api/plans/{plan_date}")
def get_plan(plan_date: Date) -> dict:
    plan = JobsRepository().active_plan(plan_date)
    if plan is None:
        raise HTTPException(404, "No plan for that date")
    repo = JobsRepository()
    return {
        **_plan_to_dict(plan),
        "stops": [
            {
                "sequence_index": s.sequence_index + 1,
                "job_id": s.job_id,
                "customer_name": (repo.get_job(s.job_id).customer_name if repo.get_job(s.job_id) else "?"),
                "arrival": s.arrival_window.start.strftime("%H:%M"),
                "departure": s.arrival_window.end.strftime("%H:%M"),
                "drive_minutes_from_prev": s.drive_minutes_from_prev,
            }
            for s in plan.sequence.stops
        ],
    }


@app.get("/api/plans/{plan_date}/versions")
def list_plan_versions(plan_date: Date) -> list[dict]:
    """Every version of a day's plan, oldest first -- the v1-vs-v2 comparison."""
    return [_plan_to_dict(p) for p in JobsRepository().plan_versions(plan_date)]


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
