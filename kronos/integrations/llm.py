"""LLM integration module with support for multiple providers.

This module provides a unified interface for interacting with various LLM
providers including Gemini, Claude, DeepSeek, and local models.
It also includes the Diagnoser class for incident diagnosis.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional, Dict, Any

from kronos.config import Config
from kronos.integrations.llm_providers import (
    LLMProvider,
    GeminiProvider,
    ClaudeProvider,
    DeepSeekProvider,
    LocalProvider,
)
from kronos.models import Diagnosis, Priority

log = logging.getLogger("kronos.integrations.llm")

# System prompt for diagnosis
_SYSTEM = """You are Kronos, an autonomous incident-response engineer.
You receive error logs and the minimal relevant code context for a software
incident. Diagnose the root cause precisely. You MUST respond with a single
JSON object and nothing else — no markdown fences, no prose around it.
Keep all fields concise; reasoning under 80 words."""

# Schema for diagnosis response
_DIAG_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "confidence": {"type": "number"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "fix_summary": {"type": "string"},
        "proposed_test": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["root_cause", "confidence", "priority"],
}

# Schema for fix generation
_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "action": {"type": "string", "enum": ["modify", "create"]},
                    "content": {"type": "string"},
                },
                "required": ["path", "action", "content"],
            },
        },
        "test_files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "required": ["summary", "files"],
    "propertyOrdering": ["summary", "files", "test_files"],
}

_PROMPT_TEMPLATE = """Analyze this incident and respond with a JSON object.

Service: {service}

Context:
{context}

{hint_block}

Response must be a JSON object with these fields:
- root_cause: brief explanation of what's wrong
- confidence: number from 0.0 to 1.0
- priority: "high", "medium", or "low"
- fix_summary: brief description of how to fix it
- proposed_test: a test that would reproduce this issue
- reasoning: brief reasoning (under 80 words)

Respond ONLY with valid JSON, no other text."""


def _repair_truncated_json(text: str) -> str:
    """Best-effort repair of JSON cut off by an LLM token limit.

    Closes an unterminated string, drops a trailing comma, and balances
    any open braces / brackets. Imperfect but rescues the leading fields
    (root_cause, confidence, priority) that we care about most.
    """
    text = text.strip()
    if not text or not text.startswith("{"):
        return text
    # If we're inside a string (odd number of unescaped quotes), close it.
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        text += '"'
    # Drop trailing comma / colon, then balance braces & brackets.
    text = text.rstrip().rstrip(",").rstrip(":").rstrip()
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")
    if open_brackets > 0:
        text += "]" * open_brackets
    if open_braces > 0:
        text += "}" * open_braces
    return text


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a possibly-noisy or truncated response."""
    text = text.strip()

    # Remove markdown code blocks
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

    # If there's still markdown, try to extract JSON from between triple backticks
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)

    # Try to find JSON object even if there's extra text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try parsing line by line to find JSON
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("{"):
            # Try to find the closing brace
            combined = " ".join(lines[i:])
            try:
                start = combined.find("{")
                end = combined.rfind("}")
                if end > start:
                    return json.loads(combined[start : end + 1])
            except:
                continue

    # Last resort: repair a truncated payload
    repaired = _repair_truncated_json(text)
    return json.loads(repaired)


def create_llm_provider(config: Optional[Config] = None, **kwargs) -> LLMProvider:
    """Create an LLM provider based on configuration.

    Args:
        config: Optional Config object. If not provided, uses kwargs.
        **kwargs: Direct provider configuration (overrides config if provided)

    Returns:
        LLMProvider instance

    Raises:
        ValueError: If provider type is unknown or required config is missing
    """
    # If config is provided, extract LLM config
    if config:
        llm_config = config.llm
        provider_type = llm_config.get("provider", "gemini").lower()

        # Common configs
        model = llm_config.get("model", "gemini-2.0-flash")
        max_tokens = llm_config.get("max_tokens", 4000)
        temperature = llm_config.get("temperature", 0.2)
        timeout = llm_config.get("timeout", 60)
        api_key = llm_config.get("api_key")
        base_url = llm_config.get("base_url")
    else:
        # Use kwargs directly
        provider_type = kwargs.get("provider", "gemini").lower()
        model = kwargs.get("model", "gemini-2.0-flash")
        max_tokens = kwargs.get("max_tokens", 4000)
        temperature = kwargs.get("temperature", 0.2)
        timeout = kwargs.get("timeout", 60)
        api_key = kwargs.get("api_key")
        base_url = kwargs.get("base_url")

    # Create provider
    if provider_type == "gemini":
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "Gemini API key required. Set GEMINI_API_KEY env var or provide in config."
            )
        return GeminiProvider(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    elif provider_type == "claude":
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "Claude API key required. Set ANTHROPIC_API_KEY env var or provide in config."
            )
        return ClaudeProvider(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    elif provider_type == "deepseek":
        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                "DeepSeek API key required. Set DEEPSEEK_API_KEY env var or provide in config."
            )
        return DeepSeekProvider(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    elif provider_type == "local":
        base_url = base_url or os.environ.get(
            "LOCAL_LLM_URL", "http://localhost:8080/v1"
        )
        # FIXED: Changed 'returns' to 'return'
        return LocalProvider(
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )

    else:
        raise ValueError(f"Unknown LLM provider: {provider_type}")


class LLMClient:
    """Wrapper class for LLM provider with additional utilities."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    @classmethod
    def from_config(cls, config: Config) -> "LLMClient":
        """Create LLMClient from config."""
        provider = create_llm_provider(config)
        return cls(provider)

    async def generate(
        self,
        prompt: str,
        system_prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Generate a response using the configured provider."""
        return await self.provider.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def close(self) -> None:
        """Clean up resources."""
        await self.provider.close()

    async def generate_json(
        self,
        prompt: str,
        system_prompt: str,
        schema: Dict[str, Any],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Generate a JSON response with schema validation."""
        response = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return json.loads(response)


class Diagnoser:
    """Main diagnosis class that uses LLM providers for incident analysis."""

    def __init__(self, config: Config):
        """Initialize Diagnoser with configuration.

        Args:
            config: Kronos configuration object
        """
        # Create the LLM provider from config
        self.llm = create_llm_provider(config)
        self.language = config.code_style.get("language", "go")
        # Store schemas for reference
        self.diag_schema = _DIAG_SCHEMA
        self.fix_schema = _FIX_SCHEMA

    async def close(self) -> None:
        """Clean up LLM provider resources."""
        await self.llm.close()

    async def _call(self, prompt: str, *, schema: Optional[dict] = None) -> str:
        """Call the LLM provider with system prompt and schema."""
        return await self.llm.generate(
            prompt=prompt,
            system_prompt=_SYSTEM,
            schema=schema,
        )

    async def diagnose(
        self, *, service: str, context: str, fuzzy_hint: Optional[str] = None
    ) -> Diagnosis:
        """Diagnose an incident from context and optional fuzzy hint."""
        hint_block = ""
        if fuzzy_hint:
            hint_block = (
                f"A similar past incident was found in memory. Treat this as a "
                f"prior, not ground truth — verify it against the context:\n"
                f"  PRIOR: {fuzzy_hint}\n\n"
            )
        prompt = _PROMPT_TEMPLATE.format(
            service=service,
            context=context or "(no context retrieved)",
            language=self.language,
            hint_block=hint_block,
        )

        # Add explicit instruction to return valid JSON
        prompt += "\n\nRespond with ONLY valid JSON, no markdown, no code blocks, no extra text."

        text = await self._call(prompt, schema=_DIAG_SCHEMA)

        try:
            parsed = _extract_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Failed to parse diagnosis JSON: %s\nRaw: %s", e, text[:1000])
            return Diagnosis(
                root_cause="Diagnosis parse failure; manual review required.",
                confidence=0.0,
                priority=Priority.LOW,
                reasoning=text[:1000],
            )

        try:
            prio = Priority(str(parsed.get("priority", "low")).lower())
        except ValueError:
            prio = Priority.LOW

        return Diagnosis(
            root_cause=parsed.get("root_cause", ""),
            reasoning=parsed.get("reasoning", ""),
            confidence=float(parsed.get("confidence", 0.0)),
            priority=prio,
            proposed_test=parsed.get("proposed_test", ""),
            fix_summary=parsed.get("fix_summary", ""),
            fuzzy_hint_used=bool(fuzzy_hint),
        )

    async def generate_fix(
        self, *, context: str, diagnosis: Diagnosis, prior_failure: Optional[str] = None
    ) -> dict:
        """Generate a code fix + regression test. Returns parsed JSON dict."""
        retry_block = ""
        if prior_failure:
            retry_block = (
                f"\nA previous attempt failed. Test/build output:\n"
                f"{prior_failure[:1500]}\nFix the issues. ENSURE the code is complete and properly indented.\n"
            )

        prompt = f"""Given this diagnosis of a {self.language} incident:

    ROOT CAUSE: {diagnosis.root_cause}
    FIX PLAN: {diagnosis.fix_summary}
    PROPOSED CONFIRMATION TEST: {diagnosis.proposed_test}

    And this code context:
    <context>
    {context}
    </context>
    {retry_block}

    Generate a COMPLETE, PROPERLY INDENTED fix and a regression test.

    IMPORTANT:
    - All code must be syntactically correct with proper indentation
    - Include complete function/class definitions with bodies
    - The test file must import the necessary modules
    - Response MUST be valid JSON with NO additional text

    Respond with ONLY a JSON object:
    {{
    "files": [
        {{"path": "main.py", "action": "modify", "content": "<COMPLETE file content with proper indentation>"}}
    ],
    "test_files": [
        {{"path": "test_main.py", "content": "<COMPLETE test file content>"}}
    ],
    "summary": "<plain-language PR description>"
    }}"""

        text = await self._call(prompt, schema=_FIX_SCHEMA)
        try:
            result = _extract_json(text)
            # Validate that files have proper content
            for file in result.get("files", []):
                if not file.get("content", "").strip():
                    log.error("Empty file content in fix generation")
                    raise ValueError("Generated file has empty content")
            return result
        except Exception as e:
            log.error(
                f"Failed to parse fix generation response: {e}\nRaw: {text[:1000]}"
            )
            # Return a minimal valid response
            return {
                "summary": "Failed to generate fix, please review manually.",
                "files": [],
                "test_files": [],
            }
