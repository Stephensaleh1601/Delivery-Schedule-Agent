"""What a published plan and a candidate evaluation report about the day.

These back the screens a judge reads: distance on a versioned plan, the before/after decomposition
of a route impact, and the coordinates a map needs. The interesting assertions are the ones about
*not* claiming precision -- a plan with no recorded distance must say so rather than showing zero.
"""
from datetime import date, time

import pytest
from fastapi.testclient import TestClient

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    Address,
    AvailabilityOption,
    DaySequence,
    JobRecord,
    JobType,
    PlanningStatus,
    RoutePlanVersion,
    StopAssignment,
    TimeWindow,
)
from dispatch_agent.planning import plan_service
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


@pytest.fixture()
def client(temp_db):
    from dispatch_agent.webapp.main import app

    return TestClient(app)


def _w(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _confirmed(repo, name, postal, day, window=((9, 0), (18, 0)), duration=45):
    job = JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal, coordinates=postal_code_to_coords(postal)),
        job_type=JobType.SOFA,
        availability=[_w(*window)],
        locked_window=_w(*window),
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        raw_message="seed",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


# -- day completion -----------------------------------------------------------


def test_completion_is_the_last_departure_plus_the_drive_home():
    sequence = DaySequence(
        delivery_date=BASE,
        stops=[
            StopAssignment(job_id="a", sequence_index=0, arrival_window=_w((9, 0), (9, 45))),
            StopAssignment(job_id="b", sequence_index=1, arrival_window=_w((14, 0), (15, 30))),
        ],
        total_drive_minutes=30,
        return_drive_minutes=20,
    )

    assert sequence.completion_minutes == 15 * 60 + 30 + 20  # 15:50


def test_an_empty_day_finishes_at_nothing():
    assert DaySequence(delivery_date=BASE, stops=[], total_drive_minutes=0).completion_minutes == 0


def test_overtime_is_measured_from_the_same_completion_time():
    """scoring and the UI must never disagree about when the day ends."""
    from dispatch_agent.planning.scoring import ScoringConfig, overtime_minutes

    sequence = DaySequence(
        delivery_date=BASE,
        stops=[StopAssignment(job_id="a", sequence_index=0, arrival_window=_w((16, 0), (17, 30)))],
        total_drive_minutes=10,
        return_drive_minutes=20,
    )
    config_ = ScoringConfig(soft_day_end=time(17, 0))

    # Finishes 17:50; soft end 17:00 -> 50 minutes over.
    assert sequence.completion_minutes == 17 * 60 + 50
    assert overtime_minutes(sequence, {}, None, config_) == 50


# -- distance -----------------------------------------------------------------


def test_a_published_plan_records_distance_including_the_drive_home(temp_db):
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "A", "018956", day)
    _confirmed(temp_db, "B", "486123", day)

    plan = plan_service.replan_day(temp_db, day, reason="initial")

    assert plan.sequence.distance_recorded is True
    assert plan.sequence.return_distance_km > 0, "the drive home was not measured"
    assert plan.sequence.round_trip_distance_km > plan.sequence.total_distance_km
    assert all(s.distance_km_from_prev > 0 for s in plan.sequence.stops)


def test_a_plan_published_before_distance_existed_says_so():
    """The reason distance_recorded is a flag rather than an inferred zero: a UI must be able to
    tell "no distance recorded" from "no distance travelled"."""
    legacy = RoutePlanVersion.model_validate_json(
        '{"id":"p1","delivery_date":"2026-09-04","version":1,"status":"active",'
        '"sequence":{"delivery_date":"2026-09-04","stops":[],"total_drive_minutes":42},'
        '"reason_created":"before distance","content_hash":"h"}'
    )

    assert legacy.sequence.distance_recorded is False
    assert legacy.sequence.total_distance_km == 0.0
    assert legacy.sequence.total_drive_minutes == 42


def test_recording_distance_does_not_bump_the_plan_version(temp_db):
    """content_hash covers who is stopped where, not how far the van drove -- so adding distance
    must not make every replan look like a change."""
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "A", "018956", day)

    first = plan_service.replan_day(temp_db, day, reason="initial")
    again = plan_service.replan_day(temp_db, day, reason="nothing changed")

    assert again.id == first.id
    assert len(temp_db.plan_versions(day)) == 1


def test_the_route_plan_endpoint_and_the_plan_agree_about_distance(client, temp_db):
    """These used to be two calculations. The endpoint's own version omitted the return leg."""
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "A", "018956", day)
    _confirmed(temp_db, "B", "486123", day)

    body = client.post("/api/route-plan", json={"date": day.isoformat()}).json()
    versions = client.get(f"/api/plans/{day}/versions").json()

    assert body["total_distance_km"] == versions[-1]["total_distance_km"]
    assert body["round_trip_distance_km"] == versions[-1]["round_trip_distance_km"]


# -- the map payload ----------------------------------------------------------


def test_a_plan_gives_a_map_everything_it_needs(client, temp_db):
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "A", "018956", day)
    client.post("/api/route-plan", json={"date": day.isoformat()})

    plan = client.get(f"/api/plans/{day}").json()

    assert plan["depot"]["lat"] and plan["depot"]["lng"]
    stop = plan["stops"][0]
    for field in ("lat", "lng", "postal_code", "job_type", "duration_minutes",
                  "readiness_status", "planning_status", "precise_location"):
        assert field in stop, f"the map payload is missing {field}"
    assert stop["lat"] and stop["lng"]
    assert plan["finishes_at"], "a route needs a finishing time"


def test_a_district_centre_pin_is_marked_approximate(client, temp_db):
    """Tests run with geocoding off, so these are all centroids -- and must admit it."""
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "A", "018956", day)
    client.post("/api/route-plan", json={"date": day.isoformat()})

    plan = client.get(f"/api/plans/{day}").json()

    assert plan["stops"][0]["precise_location"] is False


# -- route impact -------------------------------------------------------------


def test_an_evaluation_reports_the_day_before_and_after(client, temp_db):
    day = PlanningClock.horizon_dates()[0]
    for i in range(3):
        _confirmed(temp_db, f"Existing {i}", "486123", day)

    order_id = client.post("/api/orders", json={
        "customer_name": "Mrs Lee", "address_raw": "Blk 1", "postal_code": "469123",
        "job_type": "sofa",
        "availability": [{"date": day.isoformat(), "window_start": "09:00", "window_end": "18:00"}],
    }).json()["id"]
    body = client.post(f"/api/orders/{order_id}/plan-agentic").json()

    impact = body["evaluations"][0]["route_impact"]
    assert impact["stops"] == {"before": 3, "after": 4}
    assert impact["drive_minutes"]["after"] >= impact["drive_minutes"]["before"]
    assert impact["finishes_at"]["before"] and impact["finishes_at"]["after"]
    assert impact["opens_empty_day"] is False
    assert impact["empty_day_overhead_minutes"] == 0


def test_opening_an_empty_day_has_no_before_finishing_time(client, temp_db):
    """"This day did not exist yet" is the honest rendering -- not 00:00."""
    empty_day = PlanningClock.horizon_dates()[2]

    order_id = client.post("/api/orders", json={
        "customer_name": "Mr Rajan", "address_raw": "Blk 2", "postal_code": "018956",
        "job_type": "sofa",
        "availability": [{"date": empty_day.isoformat(), "window_start": "09:00", "window_end": "18:00"}],
    }).json()["id"]
    body = client.post(f"/api/orders/{order_id}/plan-agentic").json()

    impact = body["evaluations"][0]["route_impact"]
    assert impact["opens_empty_day"] is True
    assert impact["stops"]["before"] == 0
    assert impact["finishes_at"]["before"] is None
    assert impact["empty_day_overhead_minutes"] == config.settings.day_opening_penalty_minutes


def test_the_breakdown_keys_are_unchanged(client, temp_db):
    """route_impact is a SIBLING of breakdown, not inside it -- an existing contract test asserts
    exact-set equality on these four keys."""
    day = PlanningClock.horizon_dates()[0]
    order_id = client.post("/api/orders", json={
        "customer_name": "X", "address_raw": "Blk 3", "postal_code": "018956", "job_type": "sofa",
        "availability": [{"date": day.isoformat(), "window_start": "09:00", "window_end": "18:00"}],
    }).json()["id"]
    body = client.post(f"/api/orders/{order_id}/plan-agentic").json()

    assert set(body["evaluations"][0]["breakdown"]) == {
        "incremental_drive_minutes",
        "day_opening_penalty_minutes",
        "preference_penalty_minutes",
        "overtime_penalty_minutes",
    }


# -- bootstrap ----------------------------------------------------------------


def test_bootstrap_carries_the_constants_a_ui_would_otherwise_hardcode(client):
    body = client.get("/api/bootstrap").json()

    assert body["horizon"]["today"] == BASE.isoformat()
    assert len(body["horizon"]["dates"]) == 4
    assert body["map"]["depot"]["lat"]
    operating = body["operating"]
    assert operating["work_day_start"] == "09:00"
    assert operating["soft_day_end"] == "17:00"
    assert operating["day_opening_penalty_minutes"] == config.settings.day_opening_penalty_minutes


# -- what a surface may and may not show ---------------------------------------


def _payload(name="Mrs Lee", postal_code="469123"):
    day = PlanningClock.horizon_dates()[0]
    return {
        "customer_name": name, "address_raw": "Blk 1", "postal_code": postal_code,
        "job_type": "sofa",
        "availability": [{"date": day.isoformat(), "window_start": "09:00", "window_end": "18:00"}],
    }


def test_the_evaluation_payload_carries_the_components_not_just_a_total(client):
    """The decomposition is the contract. A judge reading "Route impact: 93 min" would be right to
    check it against the map and wrong about what they found -- 60 of those minutes are an
    empty-day weight nobody drives. Every component the UI shows must arrive with its own unit."""
    order_id = client.post("/api/orders", json=_payload()).json()["id"]
    impact = client.post(f"/api/orders/{order_id}/plan-options").json()["evaluations"][0]["route_impact"]

    for key in ("drive_minutes", "distance_km", "stops", "finishes_at"):
        assert set(impact[key]) == {"before", "after"}, f"{key} must be a before/after pair"
    assert isinstance(impact["opens_empty_day"], bool), "opening a day is a yes/no, not a duration"
    assert isinstance(impact["overtime_minutes"], int)


def test_the_offer_carries_a_reason_that_names_nobody_else(client):
    """The reason is customer-facing. It is built from the solved route by route_facts, so it can
    say where the van will be -- but it must never say who else is on it."""
    first = client.post("/api/orders", json=_payload("Alice", "469123")).json()["id"]
    offer = client.post(f"/api/orders/{first}/plan-options").json()["offer"]
    slot = offer["options"][0]
    client.post(f"/api/offers/{offer['id']}/respond", json={"accepted": True, "slot_id": slot["id"]})

    second = client.post("/api/orders", json=_payload("Bob", "529536")).json()["id"]
    body = client.post(f"/api/orders/{second}/plan-options").json()

    for option in body["offer"]["options"]:
        assert option["reason"], "an offered slot with no reason is the old behaviour"
        assert "Alice" not in option["reason"]
