"""DeepSeek LLM provider (OpenAI-compatible API)."""

import json
import logging
from typing import Optional, Dict, Any

import httpx

from kronos.integrations.llm_providers.base import LLMProvider

log = logging.getLogger("kronos.llm.deepseek")

_DEEPSEEK_BASE = "https://api.deepseek.com/v1"


class DeepSeekProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
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
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature or self.temperature,
        }

        # DeepSeek supports JSON mode
        if schema is not None:
            body["response_format"] = {"type": "json_object"}
            # Also add schema to prompt for better compliance
            prompt = f"{prompt}\n\nResponse must be a valid JSON object matching this schema: {json.dumps(schema)}"
            # Update the message with the enhanced prompt
            messages[1]["content"] = prompt

        resp = await self._client.post(
            f"{_DEEPSEEK_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()

        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")

    async def close(self) -> None:
        await self._client.aclose()
