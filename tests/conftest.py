"""Shared test fixtures: a temp-file SQLite DB per test, a fake LLM that never calls Bedrock,
and two autouse fixtures that make the suite *provably* offline rather than accidentally so.

Before those autouse fixtures existed, offline-ness was incidental: `ROUTING_PROVIDER` defaulted
to "onemap" with no credentials, so RoutingClient fell through to the haversine estimate. Anyone
with a real GOOGLE_MAPS_API_KEY or ONEMAP_TOKEN in their environment (or in a .env/.env.local)
silently turned every solver test into a billable, slow, flaky network call -- tests never pass
`routing_client=` to sequence_day, so they pick up whatever `settings` says.
"""
from __future__ import annotations

import pytest

from dispatch_agent import config, db


class FakeLLM:
    """Drop-in for BedrockClaude in tests -- returns canned responses instead of calling AWS.

    `structured_response` is the single-response form the intake tests rely on.
    `structured_responses` is an optional queue for multi-call flows (e.g. an agent loop that
    makes one decision call per step); it is consumed first, then the single response is used
    as the fallback for any further calls.
    """

    def __init__(
        self,
        structured_response: dict | None = None,
        text_response: str = "Your job is scheduled.",
        structured_responses: list[dict] | None = None,
    ):
        self._structured_response = structured_response or {}
        self._text_response = text_response
        self._queue = list(structured_responses or [])

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        return self._text_response

    def extract_structured(self, system, user, tool_name, tool_schema, max_tokens=1024):
        if self._queue:
            return self._queue.pop(0)
        return self._structured_response


@pytest.fixture(autouse=True)
def offline_routing(monkeypatch):
    """Force every test onto the network-free haversine estimate, whatever the developer's
    environment says. Autouse: no test should have to remember to ask for this."""
    monkeypatch.setattr(config.settings, "routing_provider", "haversine")
    for field in ("google_maps_api_key", "onemap_token", "onemap_email", "onemap_password"):
        monkeypatch.setattr(config.settings, field, "")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Turn "we happen to be offline" into "we cannot reach the network". Any test that grows a
    real HTTP call fails loudly here instead of quietly billing someone's API account."""

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "a test attempted a real network call -- stub the provider or inject a fake client"
        )

    monkeypatch.setattr("requests.get", _blocked)
    monkeypatch.setattr("requests.post", _blocked)


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config.settings, "db_path", str(db_path))
    db.init_db(db_path)
    return db.JobsRepository()
