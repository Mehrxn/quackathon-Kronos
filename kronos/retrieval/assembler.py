"""Phase 4 & 5 — Deduplication & Ranking, then Budgeted Assembly.

- Cross-pattern score aggregation: a chunk matched by multiple patterns scores higher
- Soft token cap per chunk: oversized chunks are truncated rather than dropped
- Error-type ordering: definitions of the erroring function always lead the context
- Git recency weighted into score (not just tiebreak)
- Section headers include file paths for LLM orientation
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

from kronos.config import Config
from kronos.models import CodeChunk, ErrorPattern, Priority

# Truncate a single chunk at this many tokens before dropping it entirely.
# Avoids the cliff where a 1-token-over chunk gets silently thrown away.
_CHUNK_SOFT_TOKEN_CAP = 300


def _overlaps(a: CodeChunk, b: CodeChunk) -> bool:
    if a.file != b.file:
        return False
    return not (a.end_line < b.start_line or b.end_line < a.start_line)


def _truncate_content(content: str, max_tokens: int) -> str:
    """Hard-truncate content to approximately max_tokens (4 chars/token)."""
    limit = max_tokens * 4
    if len(content) <= limit:
        return content
    return content[:limit].rsplit("\n", 1)[0] + "\n// ... [truncated]"


class ContextAssembler:
    def __init__(self, config: Config):
        self.config = config
        self.repo_path = Path(config.repository["local_path"])
        cr = config.context_retrieval
        self.max_tokens = cr.get("max_context_tokens", 4000)
        self.allocation = cr.get(
            "token_allocation",
            {
                "error_logs": 0.30,
                "definitions": 0.35,
                "callers": 0.15,
                "keywords": 0.10,
                "git_changes": 0.10,
            },
        )
        self.chunk_soft_cap = cr.get("chunk_soft_token_cap", _CHUNK_SOFT_TOKEN_CAP)

    # --- Phase 4 -------------------------------------------------------------
    @lru_cache(maxsize=256)
    def _git_recency(self, file: str) -> int:
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo_path), "log", "-1", "--format=%ct", "--", file],
                capture_output=True, text=True, timeout=10,
            )
            return int(out.stdout.strip() or 0)
        except (subprocess.SubprocessError, ValueError):
            return 0

    def _high_priority_funcs(self, patterns: list[ErrorPattern]) -> set[str]:
        return {p.function for p in patterns if p.priority_hint == Priority.HIGH}

    def _error_funcs(self, patterns: list[ErrorPattern]) -> set[str]:
        """Functions directly named in error patterns — get the biggest boost."""
        return {p.function for p in patterns if p.function != "unknown"}

    def rank(
        self, chunks: list[CodeChunk], patterns: list[ErrorPattern]
    ) -> list[CodeChunk]:
        # --- merge overlapping (keep higher score, widen range) ---
        merged: list[CodeChunk] = []
        for ch in sorted(chunks, key=lambda c: (c.file, c.start_line)):
            placed = False
            for ex in merged:
                if _overlaps(ex, ch):
                    if ch.score > ex.score:
                        ex.score = ch.score
                        ex.category = ch.category
                        ex.content = ch.content
                    ex.start_line = min(ex.start_line, ch.start_line)
                    ex.end_line = max(ex.end_line, ch.end_line)
                    placed = True
                    break
            if not placed:
                merged.append(ch)

        # --- cross-pattern aggregation: bonus if chunk appears for >1 pattern ---
        # proxy: count how many distinct error_type keywords land in the chunk
        all_keywords: list[str] = [kw for p in patterns for kw in p.keywords]
        for ch in merged:
            hits = sum(1 for kw in all_keywords if kw in ch.content.lower())
            ch.score += min(0.2, hits * 0.02)   # cap at +0.2

        # --- priority / error-function boosts ---
        hp = self._high_priority_funcs(patterns)
        ef = self._error_funcs(patterns)
        for ch in merged:
            if ch.function in ef:
                ch.score += 0.15
            if ch.function in hp:
                ch.score += 0.10

        # --- git recency weighted into score (normalised to [0, 0.05]) ---
        timestamps = [self._git_recency(ch.file) for ch in merged]
        max_ts = max(timestamps) if timestamps else 1
        for ch, ts in zip(merged, timestamps):
            ch.score += 0.05 * (ts / max_ts) if max_ts else 0

        # cap scores at 1.0
        for ch in merged:
            ch.score = min(1.0, ch.score)

        merged.sort(key=lambda c: c.score, reverse=True)
        return merged

    # --- Phase 5 -------------------------------------------------------------
    def _git_changes(self) -> list[CodeChunk]:
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo_path), "log", "-3", "--format=%h %s", "--stat"],
                capture_output=True, text=True, timeout=10,
            )
            if out.stdout.strip():
                return [CodeChunk(
                    file="<git>", start_line=0, end_line=0,
                    content=out.stdout.strip()[:2000],
                    category="git_change", score=0.5,
                )]
        except subprocess.SubprocessError:
            pass
        return []

    def assemble(
        self,
        ranked: list[CodeChunk],
        error_logs: list[str],
        patterns: list[ErrorPattern],
    ) -> tuple[str, list[CodeChunk]]:
        budgets = {k: int(self.max_tokens * v) for k, v in self.allocation.items()}
        used_chunks: list[CodeChunk] = []
        sections: dict[str, list[str]] = {}

        # error_logs
        log_budget = budgets.get("error_logs", 0)
        spent = 0
        log_text: list[str] = []
        for line in error_logs:
            t = max(1, len(line) // 4)
            if spent + t > log_budget:
                break
            log_text.append(line)
            spent += t
        if log_text:
            sections["error_logs"] = log_text

        cat_to_key = {
            "definition": "definitions",
            "caller": "callers",
            "keyword": "keywords",
            "git_change": "git_changes",
        }
        spent_by_key = {k: 0 for k in budgets}

        # Pin: definitions of directly-erroring functions go first
        error_funcs = {p.function for p in patterns if p.function != "unknown"}
        def _sort_key(ch: CodeChunk) -> tuple[int, float]:
            pinned = ch.category == "definition" and ch.function in error_funcs
            return (0 if pinned else 1, -ch.score)

        pool = sorted(list(ranked) + self._git_changes(), key=_sort_key)

        for cat_key in ("definitions", "callers", "keywords", "git_changes"):
            budget = budgets.get(cat_key, 0)
            for ch in pool:
                if cat_to_key.get(ch.category) != cat_key:
                    continue
                t = ch.approx_tokens()
                remaining = budget - spent_by_key[cat_key]
                if t > remaining:
                    # soft truncation: include a trimmed version if it fits
                    if remaining >= self.chunk_soft_cap:
                        ch = ch.model_copy()
                        ch.content = _truncate_content(ch.content, remaining)
                        t = ch.approx_tokens()
                    else:
                        continue
                spent_by_key[cat_key] += t
                used_chunks.append(ch)
                label = f"// {ch.file}:{ch.start_line}-{ch.end_line} [{ch.category} score={ch.score:.2f}]"
                sections.setdefault(cat_key, []).append(f"{label}\n{ch.content}")

        # render — include file path in headers for LLM orientation
        parts: list[str] = []
        if sections.get("error_logs"):
            parts.append("## ERROR LOGS\n" + "\n".join(sections["error_logs"]))
        if sections.get("definitions"):
            parts.append("## FUNCTION DEFINITIONS\n" + "\n\n".join(sections["definitions"]))
        if sections.get("callers"):
            parts.append("## CALL SITES\n" + "\n\n".join(sections["callers"]))
        if sections.get("keywords"):
            parts.append("## KEYWORD CONTEXT\n" + "\n\n".join(sections["keywords"]))
        if sections.get("git_changes"):
            parts.append("## RECENT GIT CHANGES\n" + "\n\n".join(sections["git_changes"]))

        return "\n\n".join(parts), used_chunks
