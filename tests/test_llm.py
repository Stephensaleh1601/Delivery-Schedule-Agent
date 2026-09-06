"""Provider selection must be explicit, especially in the offline test suite."""

import pytest

from dispatch_agent import config
from dispatch_agent import llm


def test_none_provider_constructs_no_sdk_client(monkeypatch):
    """`none` is an off switch, not another spelling of `bedrock`."""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("an offline run tried to construct an AWS client")

    monkeypatch.setattr(config.settings, "llm_provider", "none")
    monkeypatch.setattr(llm.boto3, "client", fail_if_called)

    client = llm.build_llm_client()

    with pytest.raises(llm.LLMDisabledError, match="LLM_PROVIDER=none"):
        client.extract_structured("system", "user", "answer", {"type": "object"})


def test_unknown_provider_never_falls_through_to_bedrock(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("an invalid provider tried to construct an AWS client")

    monkeypatch.setattr(config.settings, "llm_provider", "bedrok")
    monkeypatch.setattr(llm.boto3, "client", fail_if_called)

    with pytest.raises(config.ConfigurationError, match="LLM_PROVIDER"):
        llm.build_llm_client()
