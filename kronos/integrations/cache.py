"""Pattern Cache — repeat-incident fast path.

Single tier, no embeddings. On each incident:
  1. compute fingerprint (alert type + sorted function list + error type)
  2. exact match against stored Parcle rules -> use cached fix, skip LLM
  3. else Jaccard similarity over keyword sets; above threshold -> pass as
     a hint to the LLM rather than skipping the call
  4. no match -> full retrieval + diagnosis
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from kronos.config import Config
from kronos.integrations.parcle import ParcleClient
from kronos.models import ErrorPattern

log = logging.getLogger("kronos.cache")


def compute_fingerprint(alert_type: str, patterns: list[ErrorPattern]) -> str:
    funcs = sorted({p.function for p in patterns})
    etypes = sorted({p.error_type for p in patterns})
    raw = f"{alert_type}|{','.join(funcs)}|{','.join(etypes)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class CacheLookup:
    result: str  # "exact" | "fuzzy" | "miss"
    root_cause: Optional[str] = None
    fix_template: Optional[str] = None
    similarity: float = 0.0
    fingerprint: str = ""


class PatternCache:
    def __init__(self, config: Config, parcle: ParcleClient):
        self.config = config
        self.parcle = parcle
        self.threshold = config.get("pattern_cache.similarity_threshold", 0.70)

    async def lookup(self, alert_type: str,
                     patterns: list[ErrorPattern]) -> CacheLookup:
        fp = compute_fingerprint(alert_type, patterns)
        keywords = {kw for p in patterns for kw in p.keywords}

        # 1+2: exact fingerprint match via grounded search
        exact = await self.parcle.search(
            f"incident rule for fingerprint {fp}",
            tags={"type": "rule", "fingerprint": fp},
        )
        if exact and exact.get("confidence", 0) >= 0.8 and exact.get("answer"):
            log.info("Pattern cache EXACT hit fp=%s", fp)
            return CacheLookup(
                result="exact",
                root_cause=exact["answer"],
                fix_template=exact["answer"],
                similarity=1.0,
                fingerprint=fp,
            )

        # 3: fuzzy Jaccard over stored rules' keyword sets
        best_sim = 0.0
        best_rule: Optional[dict] = None
        for rule in await self.parcle.list_rules():
            tag = rule.get("tag", {})
            rk = set((tag.get("keywords") or "").split(","))
            rk.discard("")
            sim = jaccard(keywords, rk)
            if sim > best_sim:
                best_sim, best_rule = sim, rule
        if best_rule and best_sim >= self.threshold:
            log.info("Pattern cache FUZZY hit sim=%.2f fp=%s", best_sim, fp)
            return CacheLookup(
                result="fuzzy",
                root_cause=best_rule.get("summary"),
                fix_template=best_rule.get("summary"),
                similarity=best_sim,
                fingerprint=fp,
            )

        log.info("Pattern cache MISS fp=%s", fp)
        return CacheLookup(result="miss", fingerprint=fp, similarity=best_sim)
