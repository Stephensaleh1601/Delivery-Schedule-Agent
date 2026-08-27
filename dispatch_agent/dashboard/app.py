"""Small Streamlit dashboard: the day on a map, approve / edit / reject per job, and the
reschedule flow. Every coordinator edit is logged via `reschedule.record_override` -- nothing
goes to a customer without passing through this screen first.

Run with: streamlit run dispatch_agent/dashboard/app.py
"""
from __future__ import annotations

from datetime import date as Date

import pandas as pd
import pydeck as pdk
import streamlit as st

from dispatch_agent.agents.planning_agent import run_planning
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.geo.zones import COMPANY_DEPOT
from dispatch_agent.models import JobStatus, RescheduleRequest
from dispatch_agent.reschedule import apply_reschedule, record_override

st.set_page_config(page_title="Dispatch Sequencing", layout="wide")
init_db()
repo = JobsRepository()

st.title("Dispatch Sequencing Agent")

delivery_date = st.sidebar.date_input("Delivery date", value=Date.today())
jobs = repo.jobs_for_date(delivery_date)

if not jobs:
    st.info("No jobs for this date yet. Run the intake agent (see scripts/run_demo.py) first.")
    st.stop()

sequence = repo.get_sequence(delivery_date)

if st.sidebar.button("Propose / re-solve sequence"):
    result = run_planning(jobs, delivery_date)
    sequence = result.get("sequence")
    if sequence is None:
        st.sidebar.error(result.get("error", "solver could not find a feasible sequence"))
    else:
        repo.save_sequence(sequence)
        st.session_state["messages"] = result.get("messages", {})

if sequence is None:
    st.warning("No proposed sequence yet -- click 'Propose / re-solve sequence' in the sidebar.")
    st.stop()

jobs_by_id = {j.id: j for j in jobs}
messages: dict[str, str] = st.session_state.get("messages", {})

col_map, col_list = st.columns([2, 1])

with col_map:
    path_points = [[COMPANY_DEPOT.lng, COMPANY_DEPOT.lat]]
    rows = []
    for stop in sequence.stops:
        job = jobs_by_id.get(stop.job_id)
        if job is None or job.address.coordinates is None:
            continue
        coords = job.address.coordinates
        path_points.append([coords.lng, coords.lat])
        rows.append(
            {
                "lat": coords.lat,
                "lng": coords.lng,
                "label": f"{stop.sequence_index + 1}. {job.customer_name}",
                "window": f"{stop.arrival_window.start.strftime('%H:%M')}-{stop.arrival_window.end.strftime('%H:%M')}",
            }
        )
    df = pd.DataFrame(rows)
    layers = [
        pdk.Layer("PathLayer", data=[{"path": path_points}], get_path="path", get_width=4, get_color=[0, 100, 200]),
        pdk.Layer(
            "ScatterplotLayer",
            data=df,
            get_position="[lng, lat]",
            get_radius=120,
            get_fill_color=[220, 50, 50],
            pickable=True,
        ),
    ]
    view_state = pdk.ViewState(latitude=COMPANY_DEPOT.lat, longitude=COMPANY_DEPOT.lng, zoom=11)
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=view_state, tooltip={"text": "{label}\n{window}"}))
    st.metric("Total drive time", f"{sequence.total_drive_minutes} min")

with col_list:
    st.subheader("Proposed stops")
    for stop in sequence.stops:
        job = jobs_by_id.get(stop.job_id)
        if job is None:
            continue
        with st.expander(
            f"{stop.sequence_index + 1}. {job.customer_name} "
            f"({stop.arrival_window.start.strftime('%H:%M')}-{stop.arrival_window.end.strftime('%H:%M')})"
        ):
            st.write(job.address.raw_text)
            st.write(job.job_type.value)
            drafted = messages.get(job.id)
            new_start = st.time_input("Arrival window start", value=stop.arrival_window.start, key=f"start_{job.id}")
            if drafted:
                st.text_area("Drafted message", drafted, key=f"msg_{job.id}", height=80)

            approve, edit, reject = st.columns(3)
            if approve.button("Approve", key=f"approve_{job.id}"):
                job.status = JobStatus.APPROVED
                repo.save_job(job)
                st.success("Approved.")
            if edit.button("Save edit", key=f"edit_{job.id}"):
                if new_start != stop.arrival_window.start:
                    record_override(
                        delivery_date=delivery_date,
                        job_id=job.id,
                        field_changed="arrival_window.start",
                        agent_value=stop.arrival_window.start.isoformat(),
                        coordinator_value=new_start.isoformat(),
                    )
                    st.success("Edit logged.")
            if reject.button("Reject", key=f"reject_{job.id}"):
                job.status = JobStatus.REJECTED
                repo.save_job(job)
                st.warning("Rejected.")

st.divider()
st.subheader("Reschedule")
reschedule_job_id = st.selectbox(
    "Job", options=[j.id for j in jobs], format_func=lambda jid: jobs_by_id[jid].customer_name
)
new_date = st.date_input("New delivery date", value=delivery_date, key="reschedule_date")
raw_message = st.text_input("Customer's message", value="Can we move to a different day?")

if st.button("Re-solve for this change"):
    plan = apply_reschedule(RescheduleRequest(job_id=reschedule_job_id, new_date=new_date, raw_message=raw_message))
    st.write(f"Affected customers: {len(plan.affected_job_ids)}")
    for job_id in plan.affected_job_ids:
        affected_job = jobs_by_id.get(job_id) or repo.get_job(job_id)
        st.write(f"- {affected_job.customer_name if affected_job else job_id}")
    repo.save_sequence(plan.proposed_sequence)
    st.success("New sequence proposed -- review and approve above.")
    st.rerun()
