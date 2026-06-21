"""Decision matrix — reconcile priority and choose the action path.

Resolved priority reconciles the Phase-1 pattern hint against the LLM's
independent classification: on disagreement the LLM wins provided it clears
that bucket's required_confidence; the disagreement is logged for tuning.

Routing per the matrix:
  high   -> always Fix->test->build->PR (bypasses full_autonomous)
  medium -> PR if full_autonomous else issue
  low    -> always issue

A failed confirmation test forces the issue path regardless of priority.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Optional

from kronos.config import Config
from kronos.models import Diagnosis, Priority

log = logging.getLogger("kronos.decision")


class Action(str, enum.Enum):
    AUTO_FIX = "auto_fix"      # fix -> test -> build -> PR
    ISSUE = "issue"            # confirm -> issue -> tag maintainers


@dataclass
class Decision:
    resolved_priority: Priority
    action: Action
    reason: str
    confidence_ok: bool


class DecisionEngine:
    def __init__(self, config: Config):
        self.config = config
        self.full_autonomous = config.autonomy.get("full_autonomous", False)
        self.prio_rules = config.get("rules.priority", {})

    def _required_confidence(self, prio: Priority) -> float:
        return self.prio_rules.get(prio.value, {}).get("required_confidence", 0.7)

    def reconcile_priority(self, hint: Optional[Priority],
                           diagnosis: Diagnosis) -> tuple[Priority, str]:
        llm_prio = diagnosis.priority
        if hint is None or hint == llm_prio:
            return llm_prio, "hint and LLM agree" if hint else "no hint; LLM priority"
        # disagreement: LLM wins if it clears its bucket's confidence
        req = self._required_confidence(llm_prio)
        if diagnosis.confidence >= req:
            msg = (f"DISAGREEMENT hint={hint.value} llm={llm_prio.value}; "
                   f"LLM wins (conf {diagnosis.confidence:.2f} >= {req})")
            log.info(msg)
            return llm_prio, msg
        msg = (f"DISAGREEMENT hint={hint.value} llm={llm_prio.value}; "
               f"LLM under-confident ({diagnosis.confidence:.2f} < {req}), "
               f"falling back to hint")
        log.info(msg)
        return hint, msg

    def decide(self, *, hint: Optional[Priority], diagnosis: Diagnosis,
               test_reproduced: Optional[bool]) -> Decision:
        resolved, reason = self.reconcile_priority(hint, diagnosis)
        req = self._required_confidence(resolved)
        confidence_ok = diagnosis.confidence >= req

        # confirmation test must reproduce before any auto-fix
        if test_reproduced is False:
            return Decision(resolved, Action.ISSUE,
                            "confirmation test did not reproduce; route to issue",
                            confidence_ok)

        if not confidence_ok:
            return Decision(resolved, Action.ISSUE,
                            f"confidence {diagnosis.confidence:.2f} < required {req}",
                            confidence_ok)

        bucket = self.prio_rules.get(resolved.value, {})
        auto_fix = bucket.get("auto_fix", False)

        if resolved == Priority.HIGH:
            return Decision(resolved, Action.AUTO_FIX,
                            "high priority always attempts fix", confidence_ok)
        if resolved == Priority.LOW:
            return Decision(resolved, Action.ISSUE,
                            "low priority is always issue-only", confidence_ok)
        # medium
        if auto_fix == "follow_autonomy":
            if self.full_autonomous:
                return Decision(resolved, Action.AUTO_FIX,
                                "medium + full_autonomous -> fix", confidence_ok)
            return Decision(resolved, Action.ISSUE,
                            "medium + not autonomous -> issue", confidence_ok)
        if auto_fix is True:
            return Decision(resolved, Action.AUTO_FIX, "medium auto_fix=true",
                            confidence_ok)
        return Decision(resolved, Action.ISSUE, "medium auto_fix=false",
                        confidence_ok)
