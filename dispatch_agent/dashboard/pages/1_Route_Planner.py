"""Ad-hoc route planner: add clients by address + preferred date/time, then generate a
sequenced route showing order, distance, drive time and scheduled arrival per stop.

No LLM call here -- the intake agent's job is turning free-text WhatsApp messages into
structured fields, but this form collects those fields directly, so it goes straight to the
same OR-Tools solver the rest of the app uses.

Appears as a page in the same app: streamlit run dispatch_agent/dashboard/app.py
"""
from __future__ import annotations

from datetime import date as Date, time as Time

import pandas as pd
import pydeck as pdk
import streamlit as st

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import COMPANY_DEPOT
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow
from dispatch_agent.solver import UnsolvableDayError, sequence_day

st.set_page_config(page_title="Route Planner", layout="wide")
st.title("Route Planner")
st.caption(
    "Add clients with their address and preferred date/time, then generate a sequenced route "
    "with drive distance, duration, and scheduled arrival per stop."
)

if "planner_clients" not in st.session_state:
    st.session_state.planner_clients: list[JobRecord] = []

with st.form("add_client_form", clear_on_submit=True):
    st.subheader("Add a client")
    left, right = st.columns(2)
    with left:
        customer_name = st.text_input("Customer name")
        address_raw = st.text_input("Address")
        postal_code = st.text_input("Postal code (6 digits)")
        job_type = st.selectbox("Job type", options=[t.value for t in JobType])
    with right:
        delivery_date = st.date_input("Preferred date", value=Date.today())
        avail_start = st.time_input("Preferred window start", value=Time(9, 0))
        avail_end = st.time_input("Preferred window end", value=Time(18, 0))
        duration_minutes = st.number_input(
            "Job duration (minutes)", min_value=5, value=settings.default_job_duration_minutes, step=5
        )
    submitted = st.form_submit_button("Add client")

if submitted:
    if not customer_name or not address_raw:
        st.error("Customer name and address are required.")
    elif avail_end <= avail_start:
        st.error("Preferred window end must be after start.")
    else:
        try:
            coordinates = postal_code_to_coords(postal_code)
        except ValueError as exc:
            st.error(str(exc))
        else:
            job = JobRecord(
                customer_name=customer_name,
                address=Address(raw_text=address_raw, postal_code=postal_code, coordinates=coordinates),
                job_type=JobType(job_type),
                availability=[TimeWindow(start=avail_start, end=avail_end)],
                duration_minutes=int(duration_minutes),
                delivery_date=delivery_date,
                raw_message="[manual entry via Route Planner]",
            )
            st.session_state.planner_clients.append(job)
            st.success(f"Added {customer_name}.")

st.divider()

clients: list[JobRecord] = st.session_state.planner_clients

if not clients:
    st.info("No clients added yet -- use the form above.")
    st.stop()

st.subheader(f"Pending clients ({len(clients)})")
for i, job in enumerate(clients):
    cols = st.columns([3, 3, 2, 2, 1])
    cols[0].write(f"**{job.customer_name}**")
    cols[1].write(f"{job.address.raw_text} ({job.address.postal_code})")
    cols[2].write(job.delivery_date.isoformat())
    cols[3].write(f"{job.availability[0].start.strftime('%H:%M')}-{job.availability[0].end.strftime('%H:%M')}")
    if cols[4].button("Remove", key=f"remove_{job.id}"):
        st.session_state.planner_clients.pop(i)
        st.rerun()

col_clear, col_generate = st.columns([1, 1])
if col_clear.button("Clear all"):
    st.session_state.planner_clients = []
    st.rerun()
generate = col_generate.button("Generate Route Plan", type="primary")

if generate:
    routing_client = RoutingClient()
    for plan_date in sorted({job.delivery_date for job in clients}):
        day_jobs = [j for j in clients if j.delivery_date == plan_date]
        st.divider()
        st.subheader(f"Route for {plan_date.isoformat()} -- {len(day_jobs)} stop(s)")

        try:
            sequence = sequence_day(day_jobs, plan_date, depot=COMPANY_DEPOT, routing_client=routing_client)
        except (UnsolvableDayError, ValueError) as exc:
            st.error(str(exc))
            continue

        if not sequence.stops:
            st.info("No stops sequenced for this date.")
            continue

        jobs_by_id = {j.id: j for j in day_jobs}
        rows = []
        path_points = [[COMPANY_DEPOT.lng, COMPANY_DEPOT.lat]]
        map_rows = []
        prev_coords = COMPANY_DEPOT
        for stop in sequence.stops:
            job = jobs_by_id[stop.job_id]
            coords = job.address.coordinates
            leg_km = routing_client.distance_km(prev_coords, coords)
            rows.append(
                {
                    "Order": stop.sequence_index + 1,
                    "Customer": job.customer_name,
                    "Address": job.address.raw_text,
                    "Job type": job.job_type.value,
                    "Arrival": stop.arrival_window.start.strftime("%H:%M"),
                    "Departure": stop.arrival_window.end.strftime("%H:%M"),
                    "Distance from prev (km)": leg_km,
                    "Drive time from prev (min)": stop.drive_minutes_from_prev,
                }
            )
            path_points.append([coords.lng, coords.lat])
            map_rows.append(
                {
                    "lat": coords.lat,
                    "lng": coords.lng,
                    "label": f"{stop.sequence_index + 1}. {job.customer_name}",
                    "window": f"{stop.arrival_window.start.strftime('%H:%M')}-{stop.arrival_window.end.strftime('%H:%M')}",
                }
            )
            prev_coords = coords

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        total_km = round(sum(r["Distance from prev (km)"] for r in rows), 2)
        m1, m2, m3 = st.columns(3)
        m1.metric("Total stops", len(sequence.stops))
        m2.metric("Total drive time", f"{sequence.total_drive_minutes} min")
        m3.metric("Total distance", f"{total_km} km")

        map_df = pd.DataFrame(map_rows)
        layers = [
            pdk.Layer("PathLayer", data=[{"path": path_points}], get_path="path", get_width=4, get_color=[0, 100, 200]),
            pdk.Layer(
                "ScatterplotLayer",
                data=map_df,
                get_position="[lng, lat]",
                get_radius=120,
                get_fill_color=[220, 50, 50],
                pickable=True,
            ),
        ]
        view_state = pdk.ViewState(latitude=COMPANY_DEPOT.lat, longitude=COMPANY_DEPOT.lng, zoom=11)
        st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view_state, tooltip={"text": "{label}\n{window}"}))

        if st.button(f"Save this route to the dispatch board", key=f"save_{plan_date}"):
            repo = JobsRepository()
            for job in day_jobs:
                repo.save_job(job)
            repo.save_sequence(sequence)
            st.success("Saved -- open the main Dispatch Sequencing page to review and approve.")
