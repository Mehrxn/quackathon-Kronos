"""Parcle Memory API client — long-term incident memory.

Wraps the endpoints documented at docs.parcle.ai/api/memory-api:
  POST /memory/messages   record a conversation
  POST /memory/documents  add a document
  POST /memory/search     grounded, cited search
  POST /memory/list       browse stored events

The Parcle docs explicitly state the endpoints are "illustrative of the
target design, not a wire contract", and the hosted API at api.parcle.ai
returns 404 for these paths. To keep Kronos functional (and make the
"second occurrence resolves faster" demo land), this client uses a
**remote-first, local-fallback** design:

  1. First call attempts the configured remote API.
  2. If the remote returns any error, we switch to a local SQLite-backed
     store for the rest of the session. A one-line warning is logged.
  3. The local store implements the same four endpoints with reasonable
     semantics (tag-filtered search, document/message inserts, list).

Result: when you wire up a real Parcle deployment later, just point
`parcle.api_url` at it and everything works. Until then, the local store
gives you a real pattern cache.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from kronos.config import Config

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

    # POST /memory/messages
    def insert_message(self, messages: list[dict], tags: dict) -> dict:
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        content = "\n".join(m.get("content", "") for m in messages)
        summary = content[:200].replace("\n", " ")
        with self._conn() as c:
            c.execute(
                "INSERT INTO memory VALUES (?, ?, ?, ?, ?, ?)",
                (mid, "message", summary, content, json.dumps(tags), time.time()),
            )
        return {"id": mid, "status": "accepted"}

    # POST /memory/documents
    def insert_document(self, filename: str, content: str, tags: dict) -> dict:
        did = f"doc_{uuid.uuid4().hex[:12]}"
        summary = f"{filename}: {content[:200]}".replace("\n", " ")
        with self._conn() as c:
            c.execute(
                "INSERT INTO memory VALUES (?, ?, ?, ?, ?, ?)",
                (did, "document", summary, content, json.dumps(tags), time.time()),
            )
        return {"id": did, "status": "accepted"}

    # POST /memory/list
    def list_items(self, tags: dict) -> dict:
        items: list[dict] = []
        with self._conn() as c:
            for row in c.execute(
                "SELECT id, kind, summary, content, tags_json FROM memory "
                "ORDER BY created_at DESC"
            ):
                stored = json.loads(row["tags_json"])
                if self._match_tags(stored, tags):
                    items.append({
                        "id": row["id"],
                        "type": row["kind"],
                        "summary": row["summary"],
                        "tag": stored,
                    })
        return {"items": items}

    # POST /memory/search
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
        self.api_url = pc["api_url"].rstrip("/")
        self.api_key = pc["api_key"]
        self.workspace = pc.get("workspace", "kronos")
        db_path = Path(pc.get("local_db_path", "logs/parcle_local.db"))
        self._local = _LocalStore(db_path)
        # Becomes local-only after first remote failure for the rest of this
        # process. Avoids hammering a dead endpoint on every call.
        self._remote_disabled = False
        self._client = httpx.AsyncClient(
            timeout=10,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict) -> Optional[dict]:
        """Try remote first; on any failure, switch to local SQLite forever."""
        if not self._remote_disabled:
            try:
                resp = await self._client.post(f"{self.api_url}{path}", json=body)
                if 200 <= resp.status_code < 300:
                    return resp.json()
                log.warning(
                    "Parcle remote %s returned %d; switching to local SQLite "
                    "fallback for the rest of this session", path, resp.status_code,
                )
            except httpx.HTTPError as e:
                log.warning(
                    "Parcle remote %s unreachable (%s); switching to local "
                    "SQLite fallback for the rest of this session", path, e,
                )
            self._remote_disabled = True
        try:
            return await asyncio.to_thread(self._dispatch_local, path, body)
        except Exception as e:  # noqa: BLE001
            log.error("Local Parcle store error on %s: %s", path, e)
            return None

    def _dispatch_local(self, path: str, body: dict) -> Optional[dict]:
        tags = body.get("tag", {}) or {}
        if path == "/memory/search":
            return self._local.search(body.get("query", ""), tags)
        if path == "/memory/list":
            return self._local.list_items(tags)
        if path == "/memory/messages":
            return self._local.insert_message(body.get("messages", []), tags)
        if path == "/memory/documents":
            return self._local.insert_document(
                body.get("filename", "doc"),
                body.get("content", ""),
                tags,
            )
        log.warning("Unknown Parcle path for local dispatch: %s", path)
        return None

    # --- public methods (same surface as before) ----------------------------
    async def search(self, query: str, *, tags: Optional[dict] = None) -> Optional[dict]:
        """Return {answer, confidence, citations} or None."""
        tag = {"workspace": self.workspace, **(tags or {})}
        return await self._post("/memory/search", {"query": query, "tag": tag})

    async def record_incident(self, *, fingerprint: str, summary: str,
                              root_cause: str, fix_template: str,
                              keywords: list[str], outcome: str,
                              tags: Optional[dict] = None) -> Optional[dict]:
        tag = {
            "workspace": self.workspace,
            "fingerprint": fingerprint,
            "keywords": ",".join(keywords),
            "outcome": outcome,
            **(tags or {}),
        }
        messages = [
            {"speaker": "agent", "content":
                f"Incident fingerprint {fingerprint}. "
                f"Root cause: {root_cause}. "
                f"Fix template: {fix_template}. Outcome: {outcome}."},
        ]
        return await self._post("/memory/messages",
                                {"messages": messages, "tag": tag})

    async def record_rule(self, *, fingerprint: str, root_cause: str,
                          fix_template: str, confidence: float,
                          keywords: list[str]) -> Optional[dict]:
        """Store/refresh the reusable rule document for a fingerprint."""
        body = {
            "filename": f"rule-{fingerprint}.md",
            "tag": {
                "workspace": self.workspace,
                "type": "rule",
                "fingerprint": fingerprint,
                "confidence": str(round(confidence, 3)),
                "keywords": ",".join(keywords),
            },
            "content": (
                f"# Incident rule {fingerprint}\n\n"
                f"Root cause: {root_cause}\n\n"
                f"Fix template:\n{fix_template}\n"
            ),
        }
        return await self._post("/memory/documents", body)

    async def list_rules(self) -> list[dict]:
        res = await self._post("/memory/list",
                               {"tag": {"workspace": self.workspace, "type": "rule"}})
        if res and isinstance(res.get("items"), list):
            return res["items"]
        return []
