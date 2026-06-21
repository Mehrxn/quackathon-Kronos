"""Parcle Memory API client — long-term incident memory.

Backed by the official Parcle Python SDK (`pip install parcle`), which wraps
the hosted Memory API documented at docs.parcle.ai:

  client.create_user(...)     create a per-user namespace
  client.ingest_dialog(...)   record a conversation
  client.ingest_file(...)     add a document (multipart, handled by the SDK)
  client.search(...)          grounded, cited search (SSE, handled by the SDK)
  client.list_sources(...)    browse stored sessions / files

Why the SDK instead of raw httpx
--------------------------------
The new API streams search over Server-Sent Events and uploads files as
multipart/form-data — both awkward to do by hand. The SDK handles the wire
format, retries on safe reads, and the SSE assembly, returning plain result
objects. We only adapt those objects back into the dict shapes the rest of
Kronos already expects.

Remote-first, local-fallback
----------------------------
The SDK is synchronous, so every call is wrapped in `asyncio.to_thread(...)`
to avoid blocking the FastAPI event loop. The client tries the remote SDK
first; on any exception it logs a one-line warning, sets
`self._remote_disabled = True`, and uses the local SQLite store for the rest
of the session. This keeps the "second occurrence resolves faster" demo
working even with no network / no API key.

`user_id` note
--------------
The real API isolates memory per `user_id`. Kronos stores everything under a
single namespace: the config's `workspace` string is used as the `user_id`.
Labeling within that namespace is done with `tag` (on ingest) and
`tag_filter` (on search / list).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from kronos.config import Config

try:
    # The official SDK. Imported lazily-safe: if it's not installed we still
    # run fully on the local SQLite fallback.
    from parcle import Parcle
except Exception:  # noqa: BLE001 - missing/broken SDK must not crash import
    Parcle = None  # type: ignore[assignment]

log = logging.getLogger("kronos.parcle")


# --- Local SQLite store -----------------------------------------------------


class _LocalStore:
    """SQLite-backed implementation of the four Parcle memory endpoints.

    Synchronous internally; ParcleClient calls these via asyncio.to_thread
    so they don't block the event loop.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS memory (
                    id          TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    tags_json   TEXT NOT NULL,
                    created_at  REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind)")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _match_tags(self, stored: dict, wanted: dict) -> bool:
        """All wanted keys must be present in stored with matching values."""
        for k, v in wanted.items():
            if k == "workspace":
                continue
            if str(stored.get(k, "")) != str(v):
                return False
        return True

    # mirrors ingest_dialog
    def insert_message(self, messages: list[dict], tags: dict) -> dict:
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        content = "\n".join(m.get("content", "") for m in messages)
        summary = content[:200].replace("\n", " ")
        with self._conn() as c:
            c.execute(
                "INSERT INTO memory VALUES (?, ?, ?, ?, ?, ?)",
                (mid, "session", summary, content, json.dumps(tags), time.time()),
            )
        return {"id": mid, "status": "accepted"}

    # mirrors ingest_file
    def insert_document(self, filename: str, content: str, tags: dict) -> dict:
        did = f"doc_{uuid.uuid4().hex[:12]}"
        summary = f"{filename}: {content[:200]}".replace("\n", " ")
        with self._conn() as c:
            c.execute(
                "INSERT INTO memory VALUES (?, ?, ?, ?, ?, ?)",
                (did, "file", summary, content, json.dumps(tags), time.time()),
            )
        return {"id": did, "status": "accepted"}

    # mirrors list_sources
    def list_items(self, tags: dict) -> dict:
        items: list[dict] = []
        with self._conn() as c:
            for row in c.execute(
                "SELECT id, kind, summary, content, tags_json FROM memory "
                "ORDER BY created_at DESC"
            ):
                stored = json.loads(row["tags_json"])
                if self._match_tags(stored, tags):
                    items.append(
                        {
                            "id": row["id"],
                            "type": row["kind"],
                            "summary": row["summary"],
                            "tag": stored,
                        }
                    )
        return {"items": items}

    # mirrors search
    def search(self, query: str, tags: dict) -> dict:
        items = self.list_items(tags)["items"]
        if not items:
            return {"answer": "", "confidence": 0.0, "citations": []}
        top = items[0]
        # Pull full content for the top hit so the cache layer sees the
        # actual stored answer, not just the truncated summary.
        with self._conn() as c:
            row = c.execute(
                "SELECT content FROM memory WHERE id = ?", (top["id"],)
            ).fetchone()
        answer = row["content"] if row else top["summary"]
        return {
            "answer": answer,
            "confidence": 0.9,
            "citations": [{"type": top["type"], "id": top["id"]}],
        }


# --- Public client ---------------------------------------------------------


class ParcleClient:
    def __init__(self, config: Config):
        pc = config.parcle
        self.api_key = pc["api_key"]
        self.workspace = pc.get("workspace", "kronos")
        # The real API isolates memory per user_id; Kronos uses one namespace.
        self.user_id = pc.get("user_id", self.workspace)
        self.timezone = pc.get("timezone", "UTC")

        db_path = Path(pc.get("local_db_path", "logs/parcle_local.db"))
        self._local = _LocalStore(db_path)

        # Becomes local-only after first remote failure for the rest of this
        # process. Avoids hammering a dead endpoint on every call.
        self._remote_disabled = False
        self._user_ready = False

        # Construct the SDK client. If the package is missing or construction
        # fails, drop straight to the local fallback.
        self._sdk = None
        if Parcle is None:
            log.warning(
                "parcle SDK not importable; using local SQLite fallback for "
                "the whole session"
            )
            self._remote_disabled = True
        else:
            try:
                self._sdk = Parcle(api_key=self.api_key)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "Failed to construct Parcle SDK (%s); using local SQLite "
                    "fallback for the whole session",
                    e,
                )
                self._remote_disabled = True

    async def close(self) -> None:
        # The SDK manages its own HTTP lifecycle; close it if it exposes one.
        close = getattr(self._sdk, "close", None)
        if callable(close):
            try:
                await asyncio.to_thread(close)
            except Exception:  # noqa: BLE001
                pass

    # --- fallback helper -----------------------------------------------------

    def _fail_over(self, op: str, exc: Exception) -> None:
        log.warning(
            "Parcle SDK %s failed (%s); switching to local SQLite fallback "
            "for the rest of this session",
            op,
            exc,
        )
        self._remote_disabled = True

    def _ensure_user(self) -> None:
        """Create the user namespace once. Runs inside a worker thread.

        Idempotent per the API ("create or update"). Raises on failure so the
        caller's try/except trips the fallback.
        """
        if self._user_ready:
            return
        self._sdk.create_user(
            user_id=self.user_id, name="Kronos agent", timezone=self.timezone
        )
        self._user_ready = True

    # --- SDK call bodies (run in a worker thread) ----------------------------

    def _sdk_search(self, query: str, tag_filter: dict) -> dict:
        self._ensure_user()
        result = self._sdk.search(
            user_id=self.user_id, query=query, tag_filter=tag_filter
        )
        citations = []
        for c in getattr(result, "citations", None) or []:
            citations.append(
                {
                    "type": getattr(c, "type", None),
                    "id": getattr(c, "id", None),
                }
            )
        return {
            "answer": getattr(result, "answer", "") or "",
            "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
            "citations": citations,
        }

    def _sdk_ingest_dialog(self, messages: list[dict], tag: dict) -> dict:
        self._ensure_user()
        result = self._sdk.ingest_dialog(
            user_id=self.user_id, messages=messages, tag=tag
        )
        return {"id": getattr(result, "session_id", None), "status": "accepted"}

    def _sdk_ingest_file(self, filename: str, content: str, tag: dict) -> dict:
        self._ensure_user()
        # The SDK reads the file from disk, so write a temp .md first and make
        # sure it's removed afterwards.
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=Path(filename).stem + "-",
                suffix=Path(filename).suffix or ".md",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            result = self._sdk.ingest_file(user_id=self.user_id, file=tmp_path, tag=tag)
            return {"id": getattr(result, "file_id", None), "status": "accepted"}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _sdk_list_sources(self, tag_filter: dict) -> list[dict]:
        self._ensure_user()
        page = self._sdk.list_sources(
            user_id=self.user_id, type="file", tag_filter=tag_filter
        )
        out: list[dict] = []
        for s in getattr(page, "sources", None) or []:
            out.append(
                {
                    "id": getattr(s, "id", None),
                    "type": getattr(s, "type", "file"),
                    "summary": getattr(s, "summary", "") or "",
                    "tag": getattr(s, "tag", {}) or {},
                }
            )
        return out

    # --- public methods (same surface as before) ----------------------------

    async def search(
        self, query: str, *, tags: Optional[dict] = None
    ) -> Optional[dict]:
        """Return {answer, confidence, citations} or None."""
        tag = {"workspace": self.workspace, **(tags or {})}
        if not self._remote_disabled:
            try:
                return await asyncio.to_thread(self._sdk_search, query, tag)
            except Exception as e:  # noqa: BLE001
                self._fail_over("search", e)
        try:
            return await asyncio.to_thread(self._local.search, query, tag)
        except Exception as e:  # noqa: BLE001
            log.error("Local Parcle search error: %s", e)
            return None

    async def record_incident(
        self,
        *,
        fingerprint: str,
        summary: str,
        root_cause: str,
        fix_template: str,
        keywords: list[str],
        outcome: str,
        tags: Optional[dict] = None,
    ) -> Optional[dict]:
        tag = {
            "workspace": self.workspace,
            "fingerprint": fingerprint,
            "keywords": ",".join(keywords),
            "outcome": outcome,
            **(tags or {}),
        }
        # New schema: role/content (was speaker/content).
        messages = [
            {
                "role": "assistant",
                "content": f"Incident fingerprint {fingerprint}. "
                f"Root cause: {root_cause}. "
                f"Fix template: {fix_template}. Outcome: {outcome}.",
            },
        ]
        if not self._remote_disabled:
            try:
                return await asyncio.to_thread(self._sdk_ingest_dialog, messages, tag)
            except Exception as e:  # noqa: BLE001
                self._fail_over("ingest_dialog", e)
        try:
            return await asyncio.to_thread(self._local.insert_message, messages, tag)
        except Exception as e:  # noqa: BLE001
            log.error("Local Parcle message insert error: %s", e)
            return None

    async def record_rule(
        self,
        *,
        fingerprint: str,
        root_cause: str,
        fix_template: str,
        confidence: float,
        keywords: list[str],
    ) -> Optional[dict]:
        """Store/refresh the reusable rule document for a fingerprint."""
        filename = f"rule-{fingerprint}.md"
        tag = {
            "workspace": self.workspace,
            "type": "rule",
            "fingerprint": fingerprint,
            "confidence": str(round(confidence, 3)),
            "keywords": ",".join(keywords),
        }
        content = (
            f"# Incident rule {fingerprint}\n\n"
            f"Root cause: {root_cause}\n\n"
            f"Fix template:\n{fix_template}\n"
        )
        if not self._remote_disabled:
            try:
                return await asyncio.to_thread(
                    self._sdk_ingest_file, filename, content, tag
                )
            except Exception as e:  # noqa: BLE001
                self._fail_over("ingest_file", e)
        try:
            return await asyncio.to_thread(
                self._local.insert_document, filename, content, tag
            )
        except Exception as e:  # noqa: BLE001
            log.error("Local Parcle document insert error: %s", e)
            return None

    async def list_rules(self) -> list[dict]:
        tag_filter = {"workspace": self.workspace, "type": "rule"}
        if not self._remote_disabled:
            try:
                return await asyncio.to_thread(self._sdk_list_sources, tag_filter)
            except Exception as e:  # noqa: BLE001
                self._fail_over("list_sources", e)
        try:
            res = await asyncio.to_thread(self._local.list_items, tag_filter)
            return res.get("items", [])
        except Exception as e:  # noqa: BLE001
            log.error("Local Parcle list error: %s", e)
            return []
