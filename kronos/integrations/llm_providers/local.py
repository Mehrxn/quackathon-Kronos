"""Local LLM provider (OpenAI-compatible local server)."""

import json
import logging
import os
from typing import Optional, Dict, Any

import httpx

from kronos.integrations.llm_providers.base import LLMProvider

log = logging.getLogger("kronos.llm.local")


class LocalProvider(LLMProvider):
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: str = "local-model",
        max_tokens: int = 4000,
        temperature: float = 0.2,
        timeout: int = 60,
    ):
        # Get from environment if not provided
        self.base_url = base_url or os.environ.get("LOCAL_LLM_URL", "http://localhost:8080/v1")
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

        # Add explicit JSON instruction if schema is provided
        if schema is not None:
            # Use Ollama's format parameter for structured output
            # Also add it to the prompt as a fallback
            json_instruction = f"\n\nYou MUST respond with a valid JSON object that exactly matches this schema:\n{json.dumps(schema, indent=2)}"
            messages[1]["content"] = prompt + json_instruction

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature or self.temperature,
        }

        # Ollama supports JSON format
        if schema is not None:
            body["format"] = "json"

        # Log the request for debugging
        log.debug(f"Ollama request: model={self.model}, max_tokens={body['max_tokens']}")

        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"content-type": "application/json"},
            json=body,
        )
        resp.raise_for_status()

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Log the response for debugging
        log.info(f"Ollama response (first 500 chars): {content[:500]}")
        log.info(f"Ollama response length: {len(content)}")

        return content

    async def close(self) -> None:
        await self._client.aclose()
