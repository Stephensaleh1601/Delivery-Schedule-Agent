"""Shared test fixtures: a temp-file SQLite DB per test and a fake LLM that never calls
Bedrock, so the whole suite runs offline."""
from __future__ import annotations

import pytest

from dispatch_agent import config, db


class FakeLLM:
    """Drop-in for BedrockClaude in tests -- returns canned responses instead of calling AWS."""

    def __init__(self, structured_response: dict | None = None, text_response: str = "Your job is scheduled."):
        self._structured_response = structured_response or {}
        self._text_response = text_response

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        return self._text_response

    def extract_structured(self, system, user, tool_name, tool_schema, max_tokens=1024):
        return self._structured_response


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config.settings, "db_path", str(db_path))
    db.init_db(db_path)
    return db.JobsRepository()
