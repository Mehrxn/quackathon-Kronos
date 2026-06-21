"""Phase 2 & 3 — Parallel Grep Search + Streaming Context Extraction.

Phase 2 runs grep concurrently across a worker pool for every pattern:
function definitions, call sites, and keyword fallbacks. Phase 3 stream-reads
each match (rather than loading whole files), bounding definition reads by
brace depth and caller/keyword reads by a small fixed window.

Search is implemented as a pure-Python file walk + line-regex scan rather
than shelling out to a `grep` binary, so behavior is identical on Windows,
macOS, and Linux (no WSL / Git Bash / grep-on-PATH requirement).
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from kronos.config import Config
from kronos.models import CodeChunk, ErrorPattern

# Language-aware definition patterns (standard Python regex).
_DEF_PATTERNS = {
    "go": r"func\s+(\([^)]*\)\s+)?{name}\b",
    "python": r"def\s+{name}\b",
    "javascript": r"(function\s+{name}\b|{name}\s*[:=]\s*(async\s+)?function|{name}\s*[:=]\s*\()",
    "typescript": r"(function\s+{name}\b|{name}\s*[:=]\s*(async\s+)?function|{name}\s*\()",
    "java": r"(public|private|protected|static|\s).*\b{name}\s*\(",
    "rust": r"fn\s+{name}\b",
}
_OPEN_BRACE = {"go", "javascript", "typescript", "java", "rust"}
_SRC_EXT = {
    "go": [".go"],
    "python": [".py"],
    "javascript": [".js", ".jsx"],
    "typescript": [".ts", ".tsx"],
    "java": [".java"],
    "rust": [".rs"],
}
_COMMENT_PREFIX = {
    "go": "//",
    "javascript": "//",
    "typescript": "//",
    "java": "//",
    "rust": "//",
    "python": "#",
}
# Directories never worth scanning; keeps the walk fast on large checkouts.
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}


@dataclass
class GrepMatch:
    file: str
    line: int  # 1-indexed


class CodeRetriever:
    """Phases 2-3. Operates over a checked-out repo on local disk."""

    def __init__(self, config: Config):
        self.config = config
        self.repo_path = Path(config.repository["local_path"])
        self.language = config.code_style.get("language", "go")
        cr = config.context_retrieval
        self.max_workers = cr.get("max_workers", 4)
        self.grep_timeout = cr.get("grep_timeout", 30)
        self.max_function_lines = cr.get("max_function_lines", 50)
        self.max_callers = cr.get("max_callers", 5)
        self.max_keyword_matches = cr.get("max_keyword_matches", 10)
        self.strip_comments = cr.get("strip_comments", True)

    # --- Phase 2: search -------------------------------------------------------
    def _iter_source_files(self):
        exts = tuple(_SRC_EXT.get(self.language, [".go"]))
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in files:
                if fn.endswith(exts):
                    yield os.path.join(root, fn)

    def _grep(
        self, pattern: str, *, max_count: Optional[int] = None
    ) -> list[GrepMatch]:
        """Line-based regex search across source files under repo_path.

        Pure Python — no dependency on an external `grep` binary, so this
        behaves the same on Windows as on macOS/Linux.
        """
        try:
            rx = re.compile(pattern)
        except re.error:
            return []
        matches: list[GrepMatch] = []
        for path in self._iter_source_files():
            try:
                with open(path, "r", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if rx.search(line):
                            matches.append(GrepMatch(file=path, line=lineno))
                            if max_count and len(matches) >= max_count:
                                return matches
            except OSError:
                continue
        return matches

    def _def_pattern(self, name: str) -> str:
        tmpl = _DEF_PATTERNS.get(self.language, _DEF_PATTERNS["go"])
        # name may be `(*T).Method` — grep the last component
        bare = name.split(".")[-1].strip("()*")
        return tmpl.format(name=re.escape(bare))

    def _search_one_pattern(self, pat: ErrorPattern) -> list[CodeChunk]:
        """Grep + extract for a single error pattern. Runs in a worker thread."""
        chunks: list[CodeChunk] = []
        bare = pat.function.split(".")[-1].strip("()*")
        if bare and bare != "unknown":
            # definitions
            for m in self._grep(self._def_pattern(pat.function), max_count=3):
                chunk = self._extract_definition(m, pat.function)
                if chunk:
                    chunks.append(chunk)
            # call sites
            caller_pat = r"\b" + re.escape(bare) + r"\s*\("
            for m in self._grep(caller_pat, max_count=self.max_callers + 3):
                chunk = self._extract_window(
                    m, "caller", before=5, after=5, function=pat.function
                )
                if chunk:
                    chunks.append(chunk)
                if sum(1 for c in chunks if c.category == "caller") >= self.max_callers:
                    break
        # keyword fallback context
        kw_count = 0
        for kw in pat.keywords:
            if kw_count >= self.max_keyword_matches:
                break
            for m in self._grep(r"\b" + re.escape(kw) + r"\b", max_count=2):
                chunk = self._extract_window(
                    m, "keyword", before=3, after=3, function=pat.function
                )
                if chunk:
                    chunks.append(chunk)
                    kw_count += 1
                if kw_count >= self.max_keyword_matches:
                    break
        return chunks

    # --- Phase 3: streaming extraction ---------------------------------------
    def _read_lines(self, file: str) -> list[str]:
        try:
            with open(file, "r", errors="replace") as fh:
                return fh.readlines()
        except OSError:
            return []

    def _clean(self, lines: list[str]) -> str:
        prefix = _COMMENT_PREFIX.get(self.language, "//")
        out: list[str] = []
        blank_run = False
        for ln in lines:
            stripped = ln.rstrip("\n")
            if self.strip_comments and stripped.strip().startswith(prefix):
                continue
            if not stripped.strip():
                if blank_run:
                    continue
                blank_run = True
            else:
                blank_run = False
            out.append(stripped)
        return "\n".join(out).strip("\n")

    def _extract_definition(self, m: GrepMatch, function: str) -> Optional[CodeChunk]:
        """Read forward tracking brace depth until the function closes."""
        lines = self._read_lines(m.file)
        if not lines:
            return None
        start = m.line - 1
        if start >= len(lines):
            return None
        collected: list[str] = []
        depth = 0
        started = False
        end = start
        brace_lang = self.language in _OPEN_BRACE
        for i in range(start, min(start + self.max_function_lines, len(lines))):
            line = lines[i]
            collected.append(line)
            end = i
            if brace_lang:
                depth += line.count("{") - line.count("}")
                if "{" in line:
                    started = True
                if started and depth <= 0:
                    break
            else:  # python: indentation-based, approximate by blank+dedent
                if i > start and line.strip() and not line[0].isspace() and i != start:
                    collected.pop()
                    end = i - 1
                    break
        content = self._clean(collected)
        return CodeChunk(
            file=os.path.relpath(m.file, self.repo_path),
            start_line=start + 1,
            end_line=end + 1,
            content=content,
            category="definition",
            score=1.0,
            function=function,
        )

    def _extract_window(
        self, m: GrepMatch, category: str, *, before: int, after: int, function: str
    ) -> Optional[CodeChunk]:
        lines = self._read_lines(m.file)
        if not lines:
            return None
        idx = m.line - 1
        lo = max(0, idx - before)
        hi = min(len(lines), idx + after + 1)
        content = self._clean(lines[lo:hi])
        if not content.strip():
            return None
        return CodeChunk(
            file=os.path.relpath(m.file, self.repo_path),
            start_line=lo + 1,
            end_line=hi,
            content=content,
            category=category,
            score=0.8 if category == "caller" else 0.6,
            function=function,
        )

    # --- Public entry: run all patterns concurrently -------------------------
    async def retrieve(self, patterns: list[ErrorPattern]) -> list[CodeChunk]:
        """Phase 2-3 for all patterns as one concurrent batch."""
        if not patterns:
            return []
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            tasks = [
                loop.run_in_executor(pool, self._search_one_pattern, pat)
                for pat in patterns
            ]
            results = await asyncio.gather(*tasks)
        chunks: list[CodeChunk] = []
        for r in results:
            chunks.extend(r)
        return chunks
