"""In-memory incident store (thread-safe enough for a single-process demo)."""

from __future__ import annotations

import threading
from typing import Optional

from kronos.models import Incident, IncidentStatus, Priority


class IncidentStore:
    def __init__(self) -> None:
        self._items: dict[str, Incident] = {}
        self._lock = threading.Lock()

    def add(self, incident: Incident) -> None:
        with self._lock:
            self._items[incident.incident_id] = incident

    def update(self, incident: Incident) -> None:
        with self._lock:
            self._items[incident.incident_id] = incident

    def get(self, incident_id: str) -> Optional[Incident]:
        return self._items.get(incident_id)

    def list(
        self,
        *,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Incident], int]:
        items = sorted(self._items.values(), key=lambda i: i.created_at, reverse=True)
        if status:
            items = [i for i in items if i.status.value == status]
        if priority:
            items = [
                i
                for i in items
                if i.resolved_priority and i.resolved_priority.value == priority
            ]
        total = len(items)
        return items[offset : offset + limit], total
