"""LLM clients for the two agents: a Bedrock Converse API wrapper (Claude) and an OpenAI chat
completions wrapper, both exposing the same `.complete()` / `.extract_structured()` interface.

Kept deliberately small: the agents depend on that interface, never on boto3 or the openai
package directly, so tests can swap in a fake that never touches the network, and
`build_llm_client()` can swap providers based on `LLM_PROVIDER` without the agents caring.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

import boto3

from dispatch_agent.config import ConfigurationError, settings


class LLMClient(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str: ...

    def extract_structured(
        self, system: str, user: str, tool_name: str, tool_schema: dict[str, Any], max_tokens: int = 1024
    ) -> dict[str, Any]: ...


class LLMDisabledError(RuntimeError):
    """Raised when deterministic fallback code deliberately runs without a model provider."""


class DisabledLLM:
    """Network-free client used by tests and explicit offline demo runs."""

    @staticmethod
    def _raise() -> None:
        raise LLMDisabledError("LLM_PROVIDER=none; model calls are disabled")

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        self._raise()

    def extract_structured(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        self._raise()


class BedrockClaude:
    def __init__(self, model_id: str | None = None, region: str | None = None):
        self.model_id = model_id or settings.bedrock_model_id
        self._client = boto3.client("bedrock-runtime", region_name=region or settings.aws_region)

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
        )
        blocks = response["output"]["message"]["content"]
        return "".join(b.get("text", "") for b in blocks)

    def extract_structured(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Force a single tool call and return its input -- the standard structured-output
        trick, so the intake agent never has to parse free-text JSON out of a completion."""
        response = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            toolConfig={
                "tools": [{"toolSpec": {"name": tool_name, "inputSchema": {"json": tool_schema}}}],
                "toolChoice": {"tool": {"name": tool_name}},
            },
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
        for block in response["output"]["message"]["content"]:
            if "toolUse" in block:
                return block["toolUse"]["input"]
        raise ValueError("model did not return the expected tool call")


class OpenAIChat:
    """Same interface as BedrockClaude, backed by the OpenAI API instead -- set
    LLM_PROVIDER=openai and OPENAI_API_KEY in .env to use this instead of Bedrock."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        from openai import OpenAI  # imported lazily -- a Bedrock-only install has no openai package

        self.model = model or settings.openai_model
        self._client = OpenAI(api_key=api_key or settings.openai_api_key)

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_completion_tokens=max_tokens,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""

    def extract_structured(
        self,
        system: str,
        user: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Force a single tool call, same trick as BedrockClaude.extract_structured -- the
        JSON schemas in agents/prompts.py are plain enough to hand to either provider as-is."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=[{"type": "function", "function": {"name": tool_name, "parameters": tool_schema}}],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            max_completion_tokens=max_tokens,
            temperature=0,
        )
        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            raise ValueError("model did not return the expected tool call")
        return json.loads(tool_calls[0].function.arguments)


def build_llm_client() -> LLMClient:
    """The agents call this instead of instantiating a provider class directly, so switching
    LLM_PROVIDER in .env is enough -- no code changes needed to try OpenAI instead of Bedrock."""
    if settings.llm_provider == "none":
        return DisabledLLM()
    if settings.llm_provider == "openai":
        return OpenAIChat()
    if settings.llm_provider == "bedrock":
        return BedrockClaude()
    raise ConfigurationError(
        f"LLM_PROVIDER={settings.llm_provider!r} is not supported. "
        "Use bedrock, openai, or none."
    )
