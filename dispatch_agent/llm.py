"""Thin wrapper around Bedrock's Converse API for Claude Haiku 4.5, with a forced-tool-call
helper for structured output.

Kept deliberately small: the agents depend on `.complete()` (free text) and
`.extract_structured()` (a validated dict), never on boto3 directly, so tests can swap in a
fake that never touches the network.
"""
from __future__ import annotations

from typing import Any

import boto3

from dispatch_agent.config import settings


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
