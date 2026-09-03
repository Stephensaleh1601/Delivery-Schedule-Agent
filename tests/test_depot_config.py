"""The depot, and the flag that must not lie about what the solver does.

Every drive time in the system is measured from one point. Until now that point was a module
constant, which meant an environment variable could appear to set it and change nothing -- the
`depot: Coordinates = COMPANY_DEPOT` default arguments bind at import, so the override would be
read after every caller had already captured the old value. These tests pin the accessor, not the
constant, because the accessor is the part that actually honours configuration.
"""
from datetime import date, time

import pytest

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.zones import company_depot, company_depot_address
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow
from dispatch_agent.planning.candidate_service import CandidateService

DAY = date(2026, 9, 4)


def test_the_demo_default_is_sutd():
    assert company_depot_address() == "8 Somapah Rd, Singapore 487372 (SUTD)"
    assert (round(company_depot().lat, 4), round(company_depot().lng, 3)) == (1.3409, 103.962)


def test_the_environment_actually_moves_the_depot(monkeypatch):
    """The bug this prevents: an override that reads correctly and routes from the old place."""
    monkeypatch.setattr(config.settings, "depot_lat", 1.29027)
    monkeypatch.setattr(config.settings, "depot_lng", 103.851959)
    monkeypatch.setattr(config.settings, "depot_address", "Raffles Place, Singapore 048616")

    assert company_depot().lat == 1.29027
    assert company_depot_address() == "Raffles Place, Singapore 048616"


def test_a_service_built_without_a_depot_picks_up_the_override(monkeypatch, temp_db):
    """The half that a module constant got wrong. CandidateService takes `depot=None` and resolves
    it per instance, so a moved depot moves the routes rather than only the map pin."""
    monkeypatch.setattr(config.settings, "depot_lat", 1.29027)
    monkeypatch.setattr(config.settings, "depot_lng", 103.851959)

    assert CandidateService(repo=temp_db)._depot.lat == 1.29027


def test_drive_times_are_measured_from_the_configured_depot(monkeypatch, temp_db):
    """The point of the whole exercise: the first leg of the day comes from wherever the depot is.
    A job next door to one depot and across the island from another must not cost the same."""
    job = JobRecord(
        customer_name="Mrs Lee",
        address=Address(
            raw_text="Bedok", postal_code="469123", coordinates=postal_code_to_coords("469123")
        ),
        job_type=JobType.SOFA,
        availability=[TimeWindow(start=time(9, 0), end=time(18, 0))],
        delivery_date=DAY,
        raw_message="test",
        duration_minutes=45,
    )
    temp_db.save_job(job)

    from dispatch_agent.solver import sequence_day

    near = sequence_day([job], DAY, depot=postal_code_to_coords("469123"), time_limit_seconds=1)
    far = sequence_day([job], DAY, depot=postal_code_to_coords("640500"), time_limit_seconds=1)

    assert near.round_trip_drive_minutes < far.round_trip_drive_minutes


# -- the flag ------------------------------------------------------------------


def test_return_to_depot_true_is_the_supported_configuration():
    config.validate(config.settings)  # must not raise


def test_return_to_depot_false_fails_loudly_and_says_why():
    """A flag that silently does nothing is worse than no flag. The solver builds a closed route --
    depot as node 0, start and end of the single vehicle -- and an open route needs a different
    model, not a boolean."""
    broken = config.Settings()
    broken.return_to_depot = False

    with pytest.raises(config.ConfigurationError) as exc:
        config.validate(broken)

    message = str(exc.value)
    assert "RETURN_TO_DEPOT=false is not supported" in message
    assert "OR-Tools" in message, "the message must point at what would have to change"


@pytest.mark.parametrize(
    "lat,lng,where",
    [(3.1390, 101.6869, "Kuala Lumpur"), (1.4927, 103.7414, "Johor Bahru"), (0.0, 0.0, "null island")],
)
def test_a_depot_outside_singapore_fails_at_startup(lat, lng, where):
    """Silently wrong is the failure mode here: every promise would still be computed, just from
    the wrong country."""
    broken = config.Settings()
    broken.depot_lat, broken.depot_lng = lat, lng

    with pytest.raises(config.ConfigurationError, match="outside Singapore"):
        config.validate(broken)


def test_the_env_var_parses_the_words_people_actually_write(monkeypatch):
    for falsey in ("false", "False", "0", "no", "off", " FALSE "):
        monkeypatch.setenv("RETURN_TO_DEPOT", falsey)
        assert config.Settings().return_to_depot is False, falsey
    for truthy in ("true", "True", "1", "yes"):
        monkeypatch.setenv("RETURN_TO_DEPOT", truthy)
        assert config.Settings().return_to_depot is True, truthy
