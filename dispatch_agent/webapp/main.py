"""FastAPI replacement for the Streamlit dashboard: a public client-facing intake form
(static/client.html) and a back-office dispatch dashboard (static/admin.html), talking to the
JSON API below over plain fetch() -- no build step, no frontend framework.

Run with: uvicorn dispatch_agent.webapp.main:app --reload
"""
from __future__ import annotations

from datetime import date as Date, time as Time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import COMPANY_DEPOT, COMPANY_DEPOT_ADDRESS
from dispatch_agent.models import Address, JobRecord, JobStatus, JobType, TimeWindow
from dispatch_agent.solver import UnsolvableDayError, sequence_day

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Dispatch Sequencing")
init_db()


class JobSubmission(BaseModel):
    customer_name: str
    phone: str | None = None
    address_raw: str
    postal_code: str
    job_type: JobType = JobType.DELIVERY
    delivery_date: Date
    window_start: Time
    window_end: Time
    duration_minutes: int | None = Field(default=None, gt=0)
    notes: str | None = None


class RoutePlanRequest(BaseModel):
    date: Date


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
    if payload.window_end <= payload.window_start:
        raise HTTPException(400, "Preferred window end must be after start.")
    try:
        coordinates = postal_code_to_coords(payload.postal_code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    job = JobRecord(
        customer_name=payload.customer_name,
        phone=payload.phone,
        address=Address(raw_text=payload.address_raw, postal_code=payload.postal_code, coordinates=coordinates),
        job_type=payload.job_type,
        availability=[TimeWindow(start=payload.window_start, end=payload.window_end)],
        duration_minutes=payload.duration_minutes or settings.default_job_duration_minutes,
        delivery_date=payload.delivery_date,
        raw_message="[submitted via client booking form]",
        notes=payload.notes,
    )
    JobsRepository().save_job(job)
    return {"id": job.id, "status": "ok"}


# -- Back office (back face) --------------------------------------------------


@app.get("/api/jobs")
def list_jobs(date: Date | None = None) -> list[dict]:
    repo = JobsRepository()
    jobs = repo.jobs_for_date(date) if date else repo.all_jobs()
    return [_job_to_dict(j) for j in jobs]


@app.get("/api/dates")
def list_dates() -> list[str]:
    return [d.isoformat() for d in JobsRepository().pending_dates()]


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
    stops = []
    prev_coords = COMPANY_DEPOT
    total_km = 0.0
    for stop in sequence.stops:
        job = jobs_by_id[stop.job_id]
        coords = job.address.coordinates
        leg_km = routing_client.distance_km(prev_coords, coords)
        total_km += leg_km
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
                "distance_from_prev_km": leg_km,
                "drive_minutes_from_prev": stop.drive_minutes_from_prev,
            }
        )
        prev_coords = coords
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
