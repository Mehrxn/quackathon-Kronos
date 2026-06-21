"""Retry helper for rate-limited / transient LLM calls.

Drop-in for kronos.integrations.claude (the Gemini client). The 429s in the
demo logs come from Gemini's free-tier per-minute quota: two incidents fired
seconds apart issue several generateContent calls each, blow the limit, and
the raw HTTPStatusError propagates up and fails the whole incident.

Wrap the HTTP POST inside Diagnoser._call with `retry_request(...)` so a 429
waits (honoring the Retry-After header when present) and retries instead of
crashing.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional

import httpx

log = logging.getLogger("kronos.retry")

# Status codes worth retrying: rate limit + transient server errors.
_RETRYABLE = {429, 500, 502, 503, 504}


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """Parse a Retry-After header (seconds form). Returns None if absent."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


async def retry_request(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
) -> httpx.Response:
    """Call `send()` (an async function returning a Response), retrying on
    429/5xx with exponential backoff + jitter.

    `send` must perform the request fresh each call (so the request body is
    not consumed). Returns the final Response; the caller still calls
    raise_for_status() for non-retryable errors.

    Example
    -------
        resp = await retry_request(
            lambda: self._client.post(url, json=payload)
        )
        resp.raise_for_status()
    """
    attempt = 0
    while True:
        attempt += 1
        resp = await send()
        if resp.status_code not in _RETRYABLE:
            return resp
        if attempt >= max_attempts:
            log.warning(
                "Request to %s still %d after %d attempts; giving up",
                resp.request.url,
                resp.status_code,
                attempt,
            )
            return resp  # let caller raise_for_status

        server_hint = _retry_after_seconds(resp)
        backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay = server_hint if server_hint is not None else backoff
        delay += random.uniform(0, 0.5)  # jitter to desync parallel incidents
        log.info(
            "Request to %s returned %d; retrying in %.1fs (attempt %d/%d)",
            resp.request.url,
            resp.status_code,
            delay,
            attempt,
            max_attempts,
        )
        await asyncio.sleep(delay)
