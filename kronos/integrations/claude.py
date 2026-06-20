"""Diagnosis — single chain-of-thought Gemini call per incident.

The prompt asks the model to reason step-by-step about root cause, give a
confidence score, independently classify priority, and propose a minimal
confirmation test. The response is parsed as structured JSON. If a fuzzy
cache hint is present it is injected as a prior.

Uses the Gemini API (generativelanguage.googleapis.com). The public
interface (Diagnoser.diagnose / generate_fix / close) is unchanged, so the
rest of the agent is unaffected.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from kronos.config import Config
from kronos.models import Diagnosis, Priority

log = logging.getLogger("kronos.diagnose")

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_SYSTEM = """You are Kronos, an autonomous incident-response engineer.
You receive error logs and the minimal relevant code context for a software
incident. Diagnose the root cause precisely. You MUST respond with a single
JSON object and nothing else — no markdown fences, no prose around it.
Keep all fields concise; reasoning under 80 words."""

# Schema enforced via Gemini's responseSchema. propertyOrdering puts the
# critical fields FIRST so that if the model is still cut off by a token
# limit, we lose `reasoning` (least important) instead of `root_cause` or
# `confidence`.
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
    "required": ["root_cause", "confidence", "priority", "fix_summary"],
    "propertyOrdering": ["root_cause", "confidence", "priority",
                         "fix_summary", "proposed_test", "reasoning"],
}

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

# gemini-2.5-flash is a thinking model: by default it spends most of
# maxOutputTokens on hidden reasoning BEFORE emitting any output, which is
# why responses get truncated mid-string. We disable thinking explicitly so
# every token in the budget goes to the JSON.
_THINKING_OFF = {"thinkingBudget": 0}

# Floor on output tokens — below this, diagnosis JSON routinely truncates.
_MIN_OUTPUT_TOKENS = 4000

_PROMPT_TEMPLATE = """An incident has fired for service `{service}`.

{hint_block}Below is the assembled context: error logs, function definitions,
call sites, keyword matches, and recent git changes.

<context>
{context}
</context>

Diagnose the most likely root cause. Classify priority (high/medium/low)
independently based on blast radius and severity, not on any hint. Propose
a minimal {language} test that reproduces the bug BEFORE any fix.

Keep `reasoning` under 80 words. Confidence is a float 0.0-1.0."""


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
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find outermost braces and retry on that slice.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    # Last resort: repair a truncated payload.
    repaired = _repair_truncated_json(text[start:] if start != -1 else text)
    return json.loads(repaired)


class Diagnoser:
    def __init__(self, config: Config):
        cc = config.claude
        self.api_key = cc["api_key"]
        self.model = cc["model"]
        configured = cc.get("max_tokens", 2000)
        # Enforce a floor — gemini-2.5-flash needs headroom or its JSON
        # output gets truncated mid-string (see the log we just debugged).
        self.max_tokens = max(configured, _MIN_OUTPUT_TOKENS)
        if configured < _MIN_OUTPUT_TOKENS:
            log.warning("config max_tokens=%d raised to floor %d to avoid "
                        "truncated diagnosis output", configured, _MIN_OUTPUT_TOKENS)
        self.temperature = cc.get("temperature", 0.2)
        self.timeout = cc.get("timeout", 60)
        self.language = config.code_style.get("language", "go")
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, prompt: str, *, schema: Optional[dict] = None) -> str:
        """Single Gemini generateContent call. Returns concatenated text.

        When `schema` is provided we use Gemini's responseSchema +
        responseMimeType=application/json so the model emits structured
        JSON in a predictable field order.
        """
        gen_config: dict = {
            "temperature": self.temperature,
            "maxOutputTokens": self.max_tokens,
            # Disable thinking — see _THINKING_OFF comment above.
            "thinkingConfig": _THINKING_OFF,
        }
        if schema is not None:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = schema
        body = {
            "system_instruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        url = f"{_GEMINI_BASE}/{self.model}:generateContent"
        resp = await self._client.post(
            url,
            headers={
                "x-goog-api-key": self.api_key,
                "content-type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    async def diagnose(self, *, service: str, context: str,
                       fuzzy_hint: Optional[str] = None) -> Diagnosis:
        hint_block = ""
        if fuzzy_hint:
            hint_block = (
                f"A similar past incident was found in memory. Treat this as a "
                f"prior, not ground truth — verify it against the context:\n"
                f"  PRIOR: {fuzzy_hint}\n\n"
            )
        prompt = _PROMPT_TEMPLATE.format(
            service=service, context=context or "(no context retrieved)",
            language=self.language, hint_block=hint_block,
        )
        text = await self._call(prompt, schema=_DIAG_SCHEMA)
        try:
            parsed = _extract_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Failed to parse diagnosis JSON: %s\nRaw: %s", e, text[:1000])
            return Diagnosis(
                root_cause="Diagnosis parse failure; manual review required.",
                confidence=0.0, priority=Priority.LOW,
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

    async def generate_fix(self, *, context: str, diagnosis: Diagnosis,
                           prior_failure: Optional[str] = None) -> dict:
        """Generate a code fix + regression test. Returns parsed JSON dict."""
        retry_block = ""
        if prior_failure:
            retry_block = (
                f"\nA previous attempt failed. Test/build output:\n"
                f"{prior_failure[:1500]}\nFix the issues.\n"
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
Produce the minimal patch and a regression test. Respond with ONLY a JSON object:
{{
  "files": [
    {{"path": "<relative path>", "action": "modify|create", "content": "<full new file content>"}}
  ],
  "test_files": [
    {{"path": "<relative path>", "content": "<full test file content>"}}
  ],
  "summary": "<plain-language PR description>"
}}"""
        text = await self._call(prompt, schema=_FIX_SCHEMA)
        return _extract_json(text)
