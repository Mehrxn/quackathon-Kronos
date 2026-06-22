"""Claude LLM provider (Anthropic API)."""

import json
import logging
from typing import Optional, Dict, Any

import httpx

from kronos.integrations.llm_providers.base import LLMProvider

log = logging.getLogger("kronos.llm.claude")

_CLAUDE_BASE = "https://api.anthropic.com/v1/messages"


class ClaudeProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        max_tokens: int = 4000,
        temperature: float = 0.2,
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def generate(
        self,
        prompt: str,
        system_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        # Claude doesn't support JSON schema natively, so we'll enforce via prompt
        if schema is not None:
            prompt = f"{prompt}\n\nYou MUST respond with valid JSON matching this schema: {json.dumps(schema)}"

        body = {
            "model": self.model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature or self.temperature,
        }

        resp = await self._client.post(
            _CLAUDE_BASE,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()

        data = resp.json()
        return data.get("content", [{}])[0].get("text", "")

    async def close(self) -> None:
        await self._client.aclose()
