"""Phase 4 & 5 — Deduplication & Ranking, then Budgeted Assembly.

Phase 4 merges overlapping chunks, scores by category (+priority bonus),
sorts, and tiebreaks by git recency. Phase 5 allocates the token budget
across categories and walks the ranked list per category, dropping whole
chunks that don't fit rather than truncating.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

from kronos.config import Config
from kronos.models import CodeChunk, ErrorPattern, Priority


def _overlaps(a: CodeChunk, b: CodeChunk) -> bool:
    if a.file != b.file:
        return False
    return not (a.end_line < b.start_line or b.end_line < a.start_line)


class ContextAssembler:
    def __init__(self, config: Config):
        self.config = config
        self.repo_path = Path(config.repository["local_path"])
        cr = config.context_retrieval
        self.max_tokens = cr.get("max_context_tokens", 4000)
        self.allocation = cr.get("token_allocation", {
            "error_logs": 0.30, "definitions": 0.35,
            "callers": 0.15, "keywords": 0.10, "git_changes": 0.10,
        })

    # --- Phase 4 -------------------------------------------------------------
    @lru_cache(maxsize=256)
    def _git_recency(self, file: str) -> int:
        """Unix timestamp of last commit to file; 0 if unavailable."""
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo_path), "log", "-1", "--format=%ct", "--", file],
                capture_output=True, text=True, timeout=10,
            )
            return int(out.stdout.strip() or 0)
        except (subprocess.SubprocessError, ValueError):
            return 0

    def _high_priority_funcs(self, patterns: list[ErrorPattern]) -> set[str]:
        return {
            p.function for p in patterns
            if p.priority_hint == Priority.HIGH
        }

    def rank(self, chunks: list[CodeChunk],
             patterns: list[ErrorPattern]) -> list[CodeChunk]:
        # merge overlapping (keep higher score, widen range)
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

        hp = self._high_priority_funcs(patterns)
        for ch in merged:
            if ch.function in hp:
                ch.score += 0.1

        merged.sort(key=lambda c: (c.score, self._git_recency(c.file)), reverse=True)
        return merged

    # --- Phase 5 -------------------------------------------------------------
    def _git_changes(self) -> list[CodeChunk]:
        """Recent git diff summary as a context chunk."""
        try:
            out = subprocess.run(
                ["git", "-C", str(self.repo_path), "log", "-3",
                 "--format=%h %s", "--stat"],
                capture_output=True, text=True, timeout=10,
            )
            if out.stdout.strip():
                return [CodeChunk(
                    file="<git>", start_line=0, end_line=0,
                    content=out.stdout.strip()[:2000], category="git_change", score=0.5,
                )]
        except subprocess.SubprocessError:
            pass
        return []

    def assemble(self, ranked: list[CodeChunk], error_logs: list[str],
                 patterns: list[ErrorPattern]) -> tuple[str, list[CodeChunk]]:
        """Walk ranked chunks per category within sub-budgets.

        Returns (assembled_prompt_context, chunks_actually_used).
        """
        budgets = {k: int(self.max_tokens * v) for k, v in self.allocation.items()}
        used_chunks: list[CodeChunk] = []
        sections: dict[str, list[str]] = {}

        # error_logs section (synthetic chunks)
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

        # map category -> budget key
        cat_to_key = {
            "definition": "definitions", "caller": "callers",
            "keyword": "keywords", "git_change": "git_changes",
        }
        spent_by_key = {k: 0 for k in budgets}

        pool = list(ranked) + self._git_changes()
        for cat_key in ("definitions", "callers", "keywords", "git_changes"):
            budget = budgets.get(cat_key, 0)
            for ch in pool:
                if cat_to_key.get(ch.category) != cat_key:
                    continue
                t = ch.approx_tokens()
                if spent_by_key[cat_key] + t > budget:
                    continue  # drop whole, try next
                spent_by_key[cat_key] += t
                used_chunks.append(ch)
                sections.setdefault(cat_key, []).append(
                    f"// {ch.file}:{ch.start_line}-{ch.end_line} "
                    f"[{ch.category} score={ch.score:.2f}]\n{ch.content}"
                )

        # render
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
