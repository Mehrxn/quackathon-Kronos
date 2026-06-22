"""Gemini LLM provider."""

import asyncio
import random
import logging
from typing import Optional, Dict, Any

import httpx

from kronos.integrations.llm_providers.base import LLMProvider

log = logging.getLogger("kronos.llm.gemini")

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
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
        gen_config = {
            "temperature": temperature or self.temperature,
            "maxOutputTokens": max_tokens or self.max_tokens,
        }
        if schema is not None:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = schema

        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        url = f"{_GEMINI_BASE}/{self.model}:generateContent"

        max_attempts = 5
        base_delay = 15.0
        max_delay = 60.0

        for attempt in range(1, max_attempts + 1):
            resp = await self._client.post(
                url,
                headers={
                    "x-goog-api-key": self.api_key,
                    "content-type": "application/json",
                },
                json=body,
            )

            if resp.status_code == 200:
                break

            if resp.status_code != 429 or attempt == max_attempts:
                resp.raise_for_status()

            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0.5, 2.0)

            log.warning(
                "Gemini hit 429 rate limit. Cooling down for %.1fs (Attempt %d/%d)",
                delay,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(delay)

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    async def close(self) -> None:
        await self._client.aclose()
