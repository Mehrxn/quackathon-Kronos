"""traceGrep Phase 1: Error Parsing.

Map raw log lines to structured ErrorPattern objects using a regex library
of known error phrasings (nil pointer, OOM, goroutine leak, deadlock, etc.).
Each matched line is classified into an error_type with a seed keyword list;
the function name that precedes the error is extracted via Go stack-frame
patterns (`pkg.(*Type).Method(`) or a generic `identifier:` heuristic.
Patterns are deduplicated on (function, error_type) and enriched with a
priority hint derived from config rules.

No AST, no embeddings — pure regex over raw log text.
"""

from __future__ import annotations

import re
from typing import Optional

from kronos.config import Config
from kronos.models import ErrorPattern, Priority

# Known error phrasings -> (error_type, default keywords).
# Ordered: more specific patterns first.
_KNOWN_ERRORS: list[tuple[re.Pattern, str, list[str]]] = [
    (
        re.compile(r"cache\s+overflow", re.I),
        "cache_overflow",
        ["cache", "overflow", "evict"],
    ),
    (
        re.compile(r"out\s+of\s+memory|OOM\b", re.I),
        "out_of_memory",
        ["memory", "alloc", "heap"],
    ),
    (
        re.compile(r"goroutine\s+leak", re.I),
        "goroutine_leak",
        ["goroutine", "leak", "wait"],
    ),
    (
        re.compile(r"nil\s+pointer|null\s+pointer|nil\s+dereference", re.I),
        "nil_pointer",
        ["nil", "pointer", "dereference"],
    ),
    (
        re.compile(r"connection\s+pool\s+exhausted|pool\s+exhausted", re.I),
        "pool_exhausted",
        ["connection", "pool", "exhausted"],
    ),
    (
        re.compile(r"index\s+out\s+of\s+range|slice\s+bounds", re.I),
        "index_out_of_range",
        ["index", "range", "bounds"],
    ),
    (
        re.compile(r"segmentation\s+fault|segfault|SIGSEGV", re.I),
        "segfault",
        ["segfault", "memory"],
    ),
    (re.compile(r"deadlock", re.I), "deadlock", ["deadlock", "lock", "mutex"]),
    (
        re.compile(r"race\s+condition|data\s+race", re.I),
        "race_condition",
        ["race", "concurrent"],
    ),
    (
        re.compile(r"type\s+assertion", re.I),
        "type_assertion",
        ["type", "assertion", "interface"],
    ),
    (
        re.compile(r"resource\s+leak|fd\s+leak|file\s+descriptor\s+leak", re.I),
        "resource_leak",
        ["resource", "leak", "close"],
    ),
    (re.compile(r"\bpanic\b", re.I), "panic", ["panic", "recover"]),
    (
        re.compile(r"\btimeout|timed\s+out|context\s+deadline", re.I),
        "timeout",
        ["timeout", "deadline", "context"],
    ),
    (re.compile(r"deprecat", re.I), "deprecation", ["deprecated"]),
    (re.compile(r"lint\s+error", re.I), "lint_error", ["lint"]),
    (re.compile(r"\bwarning\b", re.I), "warning", ["warning"]),
]

# Function name preceding a colon. Handles Go-style `pkg.Func:` and
# `(*Recv).Method:` and bare `funcName:`.
_FUNC_BEFORE_COLON = re.compile(
    r"(?:^|\s|\.|/)" r"(\(?\*?[A-Za-z_][\w]*\)?(?:\.[A-Za-z_]\w*)*)" r"\s*[:\(]"
)
# Go stack frame style: `package.(*Type).Method(...)`
_GO_FRAME = re.compile(r"([A-Za-z_][\w./]*\.\(?\*?\w+\)?(?:\.\w+)?)\(")

_WORD = re.compile(r"[A-Za-z_]\w{2,}")


class ErrorParser:
    def __init__(self, config: Config):
        self.config = config
        self._priority_map = self._build_priority_map(config)

    @staticmethod
    def _build_priority_map(config: Config) -> dict[str, Priority]:
        """Map each configured error pattern substring to a Priority."""
        mapping: dict[str, Priority] = {}
        prio_rules = config.get("rules.priority", {})
        for level, body in prio_rules.items():
            try:
                pr = Priority(level)
            except ValueError:
                continue
            for pat in body.get("error_patterns", []):
                mapping[pat.lower()] = pr
        return mapping

    def _priority_hint(self, raw_line: str, error_type: str) -> Optional[Priority]:
        line = raw_line.lower()
        best: Optional[Priority] = None
        for pat, pr in self._priority_map.items():
            if pat in line or pat.replace(" ", "_") == error_type:
                if best is None or pr.rank > best.rank:
                    best = pr
        return best

    def _extract_function(self, line: str) -> str:
        m = _GO_FRAME.search(line)
        if m:
            return m.group(1).split("/")[-1]
        m = _FUNC_BEFORE_COLON.search(line)
        if m:
            return m.group(1)
        return "unknown"

    def _generic_keywords(self, line: str) -> list[str]:
        stop = {"the", "and", "for", "with", "error", "failed", "level", "msg", "time"}
        words = [w.lower() for w in _WORD.findall(line)]
        seen: list[str] = []
        for w in words:
            if w not in stop and w not in seen:
                seen.append(w)
            if len(seen) >= 6:
                break
        return seen

    def parse_line(self, line: str) -> Optional[ErrorPattern]:
        line = line.strip()
        if not line:
            return None
        for rx, error_type, keywords in _KNOWN_ERRORS:
            if rx.search(line):
                func = self._extract_function(line)
                return ErrorPattern(
                    function=func,
                    error_type=error_type,
                    keywords=keywords,
                    raw_line=line,
                    priority_hint=self._priority_hint(line, error_type),
                )
        # Generic fallback: only treat as error if it smells like one.
        if re.search(r"error|fail|exception|fatal|panic", line, re.I):
            func = self._extract_function(line)
            return ErrorPattern(
                function=func,
                error_type="generic_error",
                keywords=self._generic_keywords(line),
                raw_line=line,
                priority_hint=self._priority_hint(line, "generic_error"),
            )
        return None

    def parse(self, log_lines: list[str]) -> list[ErrorPattern]:
        """Parse + dedup on (function, error_type)."""
        seen: dict[tuple[str, str], ErrorPattern] = {}
        for line in log_lines:
            pattern = self.parse_line(line)
            if pattern is None:
                continue
            key = pattern.fingerprint_part()
            if key in seen:
                # merge keywords, keep highest priority hint
                existing = seen[key]
                for kw in pattern.keywords:
                    if kw not in existing.keywords:
                        existing.keywords.append(kw)
                if pattern.priority_hint and (
                    existing.priority_hint is None
                    or pattern.priority_hint.rank > existing.priority_hint.rank
                ):
                    existing.priority_hint = pattern.priority_hint
            else:
                seen[key] = pattern
        return list(seen.values())
