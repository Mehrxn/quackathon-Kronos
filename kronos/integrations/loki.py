"""Loki client — pull detailed logs for an alert's time window."""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from kronos.config import Config

log = logging.getLogger("kronos.loki")


def _parse_retention_seconds(retention: str) -> int:
    retention = retention.strip().lower()
    mult = {"h": 3600, "m": 60, "d": 86400, "s": 1}
    if retention and retention[-1] in mult:
        try:
            return int(retention[:-1]) * mult[retention[-1]]
        except ValueError:
            pass
    return 86400


class LokiClient:
    def __init__(self, config: Config):
        lk = config.loki
        self.url = lk["url"]
        self.query_timeout = lk.get("query_timeout", 30)
        self.log_limit = lk.get("log_limit", 1000)
        self.window = _parse_retention_seconds(lk.get("retention", "24h"))
        self._client = httpx.AsyncClient(timeout=self.query_timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def query(self, service: str, *, end_ts: Optional[float] = None) -> list[str]:
        """Return raw log lines for a service over the configured window.

        Degrades to an empty list when Loki is unreachable (demo can rely on
        logs passed directly into the webhook instead).
        """
        end = end_ts or time.time()
        start = end - self.window
        logql = f'{{service="{service}"}}'
        params = {
            "query": logql,
            "start": str(int(start * 1e9)),
            "end": str(int(end * 1e9)),
            "limit": str(self.log_limit),
            "direction": "backward",
        }
        try:
            resp = await self._client.get(self.url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Loki query failed: %s", e)
            return []

        lines: list[str] = []
        for stream in data.get("data", {}).get("result", []):
            for _ts, line in stream.get("values", []):
                lines.append(line)
        return lines
