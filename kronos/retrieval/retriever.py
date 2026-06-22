"""TraceGrep Phase 2 & 3: Parallel Grep Search + Context Extraction.

For each ErrorPattern, concurrently search the source tree using pure regex
grep (no AST, no embeddings):

  - Definitions:  locate the function declaration by regex, then read forward
                  tracking brace depth to collect the full body (capped at
                  max_function_lines).
  - Callers:      find call sites with a two-level depth expansion — direct
                  callers, then callers of those callers — via fixed line
                  windows (before/after).
  - Keywords:     weighted grep of error-pattern keywords; earlier keywords in
                  the pattern list score higher.
  - Imports:      surface the import block of every file that yielded a
                  definition hit, for dependency context.

Per-file chunk caps prevent a single hot file from monopolising the budget.
All chunks are deduplicated by content hash before returning.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from kronos.config import Config
from kronos.models import CodeChunk, ErrorPattern

_DEF_PATTERNS = {
    "go": r"func\s+(\([^)]*\)\s+)?{name}\b",
    "python": r"def\s+{name}\b",
    "javascript": r"(function\s+{name}\b|{name}\s*[:=]\s*(async\s+)?function|{name}\s*[:=]\s*\()",
    "typescript": r"(function\s+{name}\b|{name}\s*([:=]\s*(async\s+)?function|\())",
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
_IMPORT_PATTERNS = {
    "python": re.compile(r"^\s*(import\s+\S+|from\s+\S+\s+import)"),
    "go": re.compile(r'^\s*"[^"]+"'),            # lines inside import block
    "javascript": re.compile(r"^\s*(import\s|require\()"),
    "typescript": re.compile(r"^\s*(import\s|require\()"),
    "rust": re.compile(r"^\s*use\s+"),
    "java": re.compile(r"^\s*import\s+"),
}
_SKIP_DIRS = {
    ".git", "node_modules", "vendor", "__pycache__",
    ".venv", "venv", "dist", "build", "target", ".idea", ".vscode",
}

# Error-type → keywords that should boost chunk score when present in content
_ERROR_BOOST_TERMS: dict[str, list[str]] = {
    "nil_pointer":       ["nil", "null", "dereference", "pointer"],
    "index_out_of_range":["index", "range", "len", "bounds", "slice"],
    "out_of_memory":     ["alloc", "malloc", "heap", "oom"],
    "goroutine_leak":    ["goroutine", "wg", "waitgroup", "chan", "leak"],
    "deadlock":          ["lock", "mutex", "rwmutex", "deadlock"],
    "race_condition":    ["sync", "atomic", "mutex", "race"],
    "timeout":           ["timeout", "deadline", "context", "cancel"],
    "pool_exhausted":    ["pool", "connection", "acquire", "exhausted"],
    "panic":             ["recover", "panic", "defer"],
    "segfault":          ["unsafe", "pointer", "cgo", "memory"],
}

# Hard cap: no single file contributes more than this many chunks
_PER_FILE_CHUNK_CAP = 4


@dataclass
class GrepMatch:
    file: str
    line: int  # 1-indexed


@dataclass
class _FileIndex:
    """Lazily built index of definition locations within one file."""
    lines: list[str]
    # map bare function name -> line number (1-indexed)
    defs: dict[str, int] = field(default_factory=dict)


class CodeRetriever:
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
        # new: per-file chunk cap (configurable, default 4)
        self.per_file_cap = cr.get("per_file_chunk_cap", _PER_FILE_CHUNK_CAP)

    # -------------------------------------------------------------------------
    # File walking + grep
    # -------------------------------------------------------------------------
    def _iter_source_files(self):
        exts = tuple(_SRC_EXT.get(self.language, [".go"]))
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in files:
                if fn.endswith(exts):
                    yield os.path.join(root, fn)

    def _grep(self, pattern: str, *, max_count: Optional[int] = None) -> list[GrepMatch]:
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
        bare = name.split(".")[-1].strip("()*")
        return tmpl.format(name=re.escape(bare))

    # -------------------------------------------------------------------------
    # Score boosting
    # -------------------------------------------------------------------------
    def _error_boost(self, content: str, error_type: str) -> float:
        """Return a score bonus [0, 0.3] based on how many error-relevant
        terms appear in the chunk content."""
        terms = _ERROR_BOOST_TERMS.get(error_type, [])
        if not terms:
            return 0.0
        hits = sum(1 for t in terms if t in content.lower())
        return min(0.3, hits * 0.06)

    # -------------------------------------------------------------------------
    # Import extraction (new)
    # -------------------------------------------------------------------------
    def _extract_imports(self, file: str) -> Optional[CodeChunk]:
        """Return a small chunk covering the import block of a file."""
        rx = _IMPORT_PATTERNS.get(self.language)
        if not rx:
            return None
        lines = self._read_lines(file)
        import_lines: list[tuple[int, str]] = []
        in_block = False  # for Go multi-line import (...)
        for i, ln in enumerate(lines):
            stripped = ln.strip()
            if self.language == "go":
                if stripped.startswith("import ("):
                    in_block = True
                if in_block:
                    import_lines.append((i, ln))
                    if stripped == ")":
                        in_block = False
                elif stripped.startswith("import "):
                    import_lines.append((i, ln))
            elif rx.match(ln):
                import_lines.append((i, ln))

        if not import_lines:
            return None
        start = import_lines[0][0]
        end = import_lines[-1][0]
        content = "".join(ln for _, ln in import_lines).strip()
        if not content:
            return None
        return CodeChunk(
            file=os.path.relpath(file, self.repo_path),
            start_line=start + 1,
            end_line=end + 1,
            content=content,
            category="keyword",   # fits keyword budget; low noise
            score=0.4,
            function=None,
        )

    # -------------------------------------------------------------------------
    # Caller-of-caller expansion (new)
    # -------------------------------------------------------------------------
    def _callers_of(self, function: str, *, depth: int = 1) -> list[GrepMatch]:
        """Find call sites up to `depth` levels above `function`."""
        bare = function.split(".")[-1].strip("()*")
        if not bare or bare == "unknown":
            return []
        matches = self._grep(
            r"\b" + re.escape(bare) + r"\s*\(",
            max_count=self.max_callers * (depth + 1),
        )
        if depth <= 1:
            return matches
        # level 2: find callers of each caller function
        extra: list[GrepMatch] = []
        seen_funcs: set[str] = {bare}
        for m in matches[:self.max_callers]:
            lines = self._read_lines(m.file)
            # walk backwards from match to find enclosing function name
            enclosing = self._enclosing_function(lines, m.line - 1)
            if enclosing and enclosing not in seen_funcs:
                seen_funcs.add(enclosing)
                extra.extend(
                    self._grep(
                        r"\b" + re.escape(enclosing) + r"\s*\(",
                        max_count=3,
                    )
                )
        return matches + extra

    def _enclosing_function(self, lines: list[str], idx: int) -> Optional[str]:
        """Walk backwards from `idx` to find the nearest function definition."""
        def_rx = re.compile(self._def_pattern("{name}").replace(
            re.escape("{name}"), r"([A-Za-z_]\w*)"
        ))
        for i in range(idx, max(0, idx - 60), -1):
            m = def_rx.search(lines[i])
            if m:
                # last capture group is the name
                return m.group(m.lastindex)
        return None

    # -------------------------------------------------------------------------
    # Core extraction
    # -------------------------------------------------------------------------
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

    def _extract_definition(
        self, m: GrepMatch, function: str, error_type: str = ""
    ) -> Optional[CodeChunk]:
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
            else:
                if i > start and line.strip() and not line[0].isspace():
                    collected.pop()
                    end = i - 1
                    break
        content = self._clean(collected)
        base_score = 1.0
        boost = self._error_boost(content, error_type)
        return CodeChunk(
            file=os.path.relpath(m.file, self.repo_path),
            start_line=start + 1,
            end_line=end + 1,
            content=content,
            category="definition",
            score=min(1.0, base_score + boost),
            function=function,
        )

    def _extract_window(
        self,
        m: GrepMatch,
        category: str,
        *,
        before: int,
        after: int,
        function: str,
        error_type: str = "",
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
        base = 0.8 if category == "caller" else 0.6
        boost = self._error_boost(content, error_type)
        return CodeChunk(
            file=os.path.relpath(m.file, self.repo_path),
            start_line=lo + 1,
            end_line=hi,
            content=content,
            category=category,
            score=min(1.0, base + boost),
            function=function,
        )

    # -------------------------------------------------------------------------
    # Per-pattern search (Phase 2+3)
    # -------------------------------------------------------------------------
    def _search_one_pattern(self, pat: ErrorPattern) -> list[CodeChunk]:
        chunks: list[CodeChunk] = []
        file_counts: dict[str, int] = {}
        error_type = pat.error_type

        def _add(chunk: Optional[CodeChunk]) -> bool:
            if chunk is None:
                return False
            n = file_counts.get(chunk.file, 0)
            if n >= self.per_file_cap:
                return False
            file_counts[chunk.file] = n + 1
            chunks.append(chunk)
            return True

        bare = pat.function.split(".")[-1].strip("()*")
        if bare and bare != "unknown":
            # definitions
            for m in self._grep(self._def_pattern(pat.function), max_count=3):
                _add(self._extract_definition(m, pat.function, error_type))

            # callers (depth-2)
            caller_matches = self._callers_of(pat.function, depth=2)
            caller_count = 0
            for m in caller_matches:
                if caller_count >= self.max_callers:
                    break
                ch = self._extract_window(
                    m, "caller", before=5, after=5,
                    function=pat.function, error_type=error_type,
                )
                if _add(ch):
                    caller_count += 1

            # import block of each file that had a definition hit
            seen_import_files: set[str] = set()
            for ch in list(chunks):
                abs_path = str(self.repo_path / ch.file)
                if abs_path not in seen_import_files:
                    seen_import_files.add(abs_path)
                    _add(self._extract_imports(abs_path))

        # keyword search — weighted by position (earlier = higher weight)
        kw_count = 0
        for rank, kw in enumerate(pat.keywords):
            if kw_count >= self.max_keyword_matches:
                break
            kw_score_bonus = max(0.0, 0.1 - rank * 0.01)  # earlier kws score higher
            for m in self._grep(r"\b" + re.escape(kw) + r"\b", max_count=2):
                ch = self._extract_window(
                    m, "keyword", before=3, after=3,
                    function=pat.function, error_type=error_type,
                )
                if ch:
                    ch.score += kw_score_bonus
                if _add(ch):
                    kw_count += 1
                if kw_count >= self.max_keyword_matches:
                    break

        return chunks

    # -------------------------------------------------------------------------
    # Dedup by content hash (new)
    # -------------------------------------------------------------------------
    @staticmethod
    def _dedup(chunks: list[CodeChunk]) -> list[CodeChunk]:
        seen: set[str] = set()
        out: list[CodeChunk] = []
        for ch in chunks:
            h = hashlib.md5(ch.content.encode(), usedforsecurity=False).hexdigest()
            if h not in seen:
                seen.add(h)
                out.append(ch)
        return out

    # -------------------------------------------------------------------------
    # Public entry
    # -------------------------------------------------------------------------
    async def retrieve(self, patterns: list[ErrorPattern]) -> list[CodeChunk]:
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
        return self._dedup(chunks)
