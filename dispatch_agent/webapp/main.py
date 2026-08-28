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
from dispatch_agent.models import DaySequence, JobRecord, JobStatus, Notification
from dispatch_agent.solver import UnsolvableDayError, sequence_day
from dispatch_agent.webapp import chat as chat_engine
from dispatch_agent.webapp.jobs_service import (
    JobSubmission,
    JobSubmissionError,
    create_job_from_submission,
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
        "delivery_date": job.delivery_date.isoformat(),
        "availability": [
            {"start": w.start.strftime("%H:%M"), "end": w.end.strftime("%H:%M")} for w in job.availability
        ],
        "duration_minutes": job.duration_minutes,
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
        {d for d in {previous_date, job.delivery_date} if _sequence_has_stops(repo.get_sequence(d))}
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

    was_sequenced = _sequence_has_stops(repo.get_sequence(job.delivery_date))
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
    repo = JobsRepository()
    jobs = repo.jobs_for_date(payload.date)
    if not jobs:
        return {"stops": [], "total_drive_minutes": 0, "total_distance_km": 0, "error": None}

    routing_client = RoutingClient()
    try:
        sequence = sequence_day(jobs, payload.date, depot=COMPANY_DEPOT, routing_client=routing_client)
    except (UnsolvableDayError, ValueError) as exc:
        return {"stops": [], "total_drive_minutes": 0, "total_distance_km": 0, "error": str(exc)}

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
        job.status = JobStatus.SEQUENCED
        repo.save_job(job)

    repo.save_sequence(sequence)
    return {
        "stops": stops,
        "total_drive_minutes": sequence.total_drive_minutes,
        "total_distance_km": round(total_km, 2),
        "error": None,
    }


# -- Static frontend -----------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def client_form() -> FileResponse:
    return FileResponse(STATIC_DIR / "client.html")


@app.get("/admin")
def admin_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")
