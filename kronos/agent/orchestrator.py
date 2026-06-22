from __future__ import annotations

"""Orchestrator — the end-to-end incident workflow (steps 1-13).

Ties retrieval, cache, diagnosis, decision matrix, and GitHub actions into
one background task per incident.
"""

import logging
import time
from typing import Optional

import asyncio
import logging
import time
from typing import Optional

from kronos.agent.decision import Action, DecisionEngine
from kronos.agent.runner import CommandRunner
from kronos.config import Config
from kronos.integrations import (
    Diagnoser,
    GitHubClient,
    LokiClient,
    ParcleClient,
    PatternCache,
)
from kronos.models import Diagnosis, Incident, IncidentStatus, Priority
from kronos.retrieval import ContextAssembler, CodeRetriever, ErrorParser

log = logging.getLogger("kronos.orchestrator")


class Orchestrator:
    def __init__(self, config: Config, store: "IncidentStore"):
        self.config = config
        self.store = store
        self.parser = ErrorParser(config)
        self.retriever = CodeRetriever(config)
        self.assembler = ContextAssembler(config)
        self.parcle = ParcleClient(config)
        self.cache = PatternCache(config, self.parcle)
        self.diagnoser = Diagnoser(config)
        self.loki = LokiClient(config)
        self.github = GitHubClient(config)
        self.decision = DecisionEngine(config)
        self.runner = CommandRunner(config)
        self.max_retries = config.autonomy.get("max_generation_retries", 3)
        self.approval_timeout = config.autonomy.get("approval_timeout", 3600)
        self.approval_interval = config.autonomy.get("approval_poll_interval", 30)
        self.full_autonomous = config.autonomy.get("full_autonomous", True)

    async def aclose(self) -> None:
        await self.parcle.close()
        await self.diagnoser.close()
        await self.loki.close()

    async def run(self, incident: Incident, *, fetch_loki: bool = True) -> None:
        try:
            await self._run_inner(incident, fetch_loki=fetch_loki)
        except Exception as e:  # noqa: BLE001 - background task must not crash
            log.exception("Incident %s failed", incident.incident_id)
            incident.status = IncidentStatus.FAILED
            incident.error = str(e)
            incident.log(f"FAILED: {e}")
            self.store.update(incident)

    async def _run_inner(self, incident: Incident, *, fetch_loki: bool) -> None:
        # Step 3: sync repo
        incident.log("Syncing repository")
        self.github.sync_repo()

        # Step 4: pull detailed logs
        logs = list(incident.error_logs)
        if fetch_loki and incident.service:
            incident.log("Pulling logs from Loki")
            logs += await self.loki.query(incident.service)
        if not logs:
            incident.log("No logs available; aborting")
            incident.status = IncidentStatus.FAILED
            incident.error = "no logs"
            self.store.update(incident)
            return

        # Parse errors
        patterns = self.parser.parse(logs)
        incident.patterns = patterns
        incident.log(f"Parsed {len(patterns)} error pattern(s)")
        alert_type = incident.prometheus_logs.get("alertname", "generic")

        # Pattern cache lookup
        incident.status = IncidentStatus.DIAGNOSING
        lookup = await self.cache.lookup(alert_type, patterns)
        incident.fingerprint = lookup.fingerprint
        incident.cache_result = lookup.result
        incident.log(f"Cache lookup: {lookup.result} (sim={lookup.similarity:.2f})")

        diagnosis: Diagnosis
        fuzzy_hint: Optional[str] = None

        # ALWAYS run diagnosis when full_autonomous is True
        if self.full_autonomous:
            incident.log("Full autonomous mode: running fresh diagnosis")

            # Get context
            chunks = await self.retriever.retrieve(patterns)
            ranked = self.assembler.rank(chunks, patterns)
            context, used = self.assembler.assemble(ranked, logs, patterns)
            incident.chunks = used
            incident.log(f"Assembled context from {len(used)} chunk(s)")

            if lookup.result == "fuzzy":
                fuzzy_hint = lookup.root_cause

            # Run diagnosis
            diagnosis = await self.diagnoser.diagnose(
                service=incident.service, context=context, fuzzy_hint=fuzzy_hint
            )
            incident.log(
                f"Diagnosed: conf={diagnosis.confidence:.2f} "
                f"prio={diagnosis.priority.value}"
            )

        elif lookup.result == "exact":
            # Use cached diagnosis
            diagnosis = Diagnosis(
                root_cause=lookup.root_cause or "",
                confidence=0.9,
                priority=self._infer_priority(patterns),
                fix_summary=lookup.fix_template or "",
                from_cache=True,
            )
            incident.log("Using cached root cause (skipped LLM)")
        else:
            # Fuzzy or miss: run diagnosis
            incident.log("Running error-based retrieval")
            chunks = await self.retriever.retrieve(patterns)
            ranked = self.assembler.rank(chunks, patterns)
            context, used = self.assembler.assemble(ranked, logs, patterns)
            incident.chunks = used
            incident.log(f"Assembled context from {len(used)} chunk(s)")

            if lookup.result == "fuzzy":
                fuzzy_hint = lookup.root_cause

            diagnosis = await self.diagnoser.diagnose(
                service=incident.service, context=context, fuzzy_hint=fuzzy_hint
            )
            incident.log(
                f"Diagnosed: conf={diagnosis.confidence:.2f} "
                f"prio={diagnosis.priority.value}"
            )

        incident.diagnosis = diagnosis

        # Step 9: confirmation test
        test_reproduced: Optional[bool] = None
        if diagnosis.proposed_test:
            incident.log("Running confirmation test")
            test_reproduced = self.runner.run_confirmation_test(diagnosis.proposed_test)
            incident.log(f"Confirmation reproduced bug: {test_reproduced}")

        # Step 8 + 10: reconcile priority
        hint = self._infer_priority(patterns)
        decision = self.decision.decide(
            hint=hint, diagnosis=diagnosis, test_reproduced=test_reproduced
        )
        incident.resolved_priority = decision.resolved_priority
        incident.log(f"Decision: {decision.action.value} — {decision.reason}")

        # FORCE AUTO-FIX IN FULL AUTONOMOUS MODE
        if self.full_autonomous:
            incident.log("Full autonomous: forcing auto-fix path")
            await self._auto_fix_path(incident, diagnosis)
        elif decision.action == Action.AUTO_FIX:
            await self._auto_fix_path(incident, diagnosis)
        else:
            await self._issue_path(incident, diagnosis, decision.reason)

        # Step 13: learn
        await self._learn(incident, diagnosis)
        self.store.update(incident)

    def _infer_priority(self, patterns) -> Priority:
        best = Priority.LOW
        for p in patterns:
            if p.priority_hint and p.priority_hint.rank > best.rank:
                best = p.priority_hint
        return best

    async def _build_context_for_fix(self, incident: Incident) -> str:
        chunks = await self.retriever.retrieve(incident.patterns)
        ranked = self.assembler.rank(chunks, incident.patterns)
        context, _ = self.assembler.assemble(
            ranked, incident.error_logs, incident.patterns
        )
        return context

    async def _auto_fix_path(self, incident: Incident, diagnosis: Diagnosis) -> None:
        incident.status = IncidentStatus.FIXING
        ts = time.strftime("%Y%m%d-%H%M%S")
        branch = f"kronos/fix-{incident.incident_id[:8]}-{ts}"
        self.github.create_branch(branch)
        context = await self._build_context_for_fix(incident)

        skip_tests = self.config.autonomy.get("skip_test_validation", False)

        prior_failure: Optional[str] = None
        success = False
        for attempt in range(1, self.max_retries + 1):
            incident.log(f"Fix generation attempt {attempt}/{self.max_retries}")
            try:
                patch = await self.diagnoser.generate_fix(
                    context=context, diagnosis=diagnosis, prior_failure=prior_failure
                )
            except Exception as e:  # noqa: BLE001
                prior_failure = f"generation error: {e}"
                continue
            self.github.apply_files(patch.get("files", []))
            self.github.apply_files(patch.get("test_files", []))

            if skip_tests:
                incident.log("Skipping test validation (config override)")
                success = True
                diagnosis.fix_summary = patch.get("summary", diagnosis.fix_summary)
                break

            test_res = self.runner.run_tests()
            if not test_res.ok:
                prior_failure = test_res.output
                incident.log("Tests failed, retrying")
                continue
            build_res = self.runner.run_build()
            if not build_res.ok:
                prior_failure = build_res.output
                incident.log("Build failed, retrying")
                continue
            success = True
            diagnosis.fix_summary = patch.get("summary", diagnosis.fix_summary)
            break

        if not success:
            incident.log("Auto-fix failed after retries; routing to issue")
            await self._issue_path(incident, diagnosis, "auto-fix failed after retries")
            return

        # Commit and push the branch, then open a PR in full autonomous mode
        commit_msg = f"fix(kronos): auto-fix for incident {incident.incident_id[:8]}\n\n{diagnosis.fix_summary}"
        self.github.commit_all(commit_msg)
        pushed = self.github.push_branch(branch)
        if not pushed:
            incident.log("Branch push failed; routing to issue")
            await self._issue_path(incident, diagnosis, "branch push failed")
            return

        if self.full_autonomous:
            incident.log("Full autonomous: opening pull request")
            pr_url = self.github.open_pr(
                branch=branch,
                title=f"[Kronos] Fix incident {incident.incident_id[:8]}",
                body=self._pr_body(incident, diagnosis),
            )
            incident.pr_url = pr_url
            incident.status = IncidentStatus.PR_OPENED
            incident.log(f"PR opened: {pr_url}")
        else:
            incident.status = IncidentStatus.RESOLVED

    async def _issue_path(
        self, incident: Incident, diagnosis: Diagnosis, reason: str
    ) -> None:
        prio = incident.resolved_priority.value if incident.resolved_priority else "?"
        title = f"[Kronos] Incident {incident.incident_id[:8]} — Priority {prio}"
        body = self._issue_body(incident, diagnosis, reason)
        url = self.github.open_issue(title=title, body=body)
        incident.issue_url = url
        incident.status = IncidentStatus.ISSUE_OPENED
        incident.log(f"Issue opened: {url}")

        # Step 12: poll for @agent: fix / ignore
        if url:
            decision = await asyncio.to_thread(
                self.github.poll_issue_for_command,
                url,
                timeout=self.approval_timeout,
                interval=self.approval_interval,
            )
            incident.log(f"Maintainer command: {decision}")
            if decision == "fix":
                incident.resolved_priority = (
                    incident.resolved_priority or Priority.MEDIUM
                )
                await self._auto_fix_path(incident, diagnosis)
            elif decision == "ignore":
                incident.status = IncidentStatus.IGNORED

    async def _learn(self, incident: Incident, diagnosis: Diagnosis) -> None:
        if not incident.fingerprint:
            return
        keywords = sorted({kw for p in incident.patterns for kw in p.keywords})
        outcome = incident.status.value
        await self.parcle.record_incident(
            fingerprint=incident.fingerprint,
            summary=diagnosis.root_cause,
            root_cause=diagnosis.root_cause,
            fix_template=diagnosis.fix_summary,
            keywords=keywords,
            outcome=outcome,
            tags={"incident_id": incident.incident_id},
        )
        await self.parcle.record_rule(
            fingerprint=incident.fingerprint,
            root_cause=diagnosis.root_cause,
            fix_template=diagnosis.fix_summary,
            confidence=diagnosis.confidence,
            keywords=keywords,
        )
        incident.log("Recorded incident + rule in Parcle memory")
        if incident.status not in (IncidentStatus.FAILED, IncidentStatus.IGNORED):
            incident.status = IncidentStatus.RESOLVED

    def _pr_body(self, incident: Incident, d: Diagnosis) -> str:
        return (
            f"## Kronos auto-generated fix\n\n"
            f"**Incident:** `{incident.incident_id}`  \n"
            f"**Resolved priority:** {incident.resolved_priority.value if incident.resolved_priority else '?'}  \n"
            f"**Confidence:** {d.confidence:.2f}\n\n"
            f"### Root cause\n{d.root_cause}\n\n"
            f"### Fix summary\n{d.fix_summary}\n\n"
            f"### Reproduction test\n```\n{d.proposed_test}\n```\n\n"
            f"_Generated by Kronos. Please review before merging._"
        )

    def _issue_body(self, incident: Incident, d: Diagnosis, reason: str) -> str:
        return (
            f"## Kronos incident report\n\n"
            f"**Incident:** `{incident.incident_id}`  \n"
            f"**Resolved priority:** {incident.resolved_priority.value if incident.resolved_priority else '?'}  \n"
            f"**Confidence:** {d.confidence:.2f}  \n"
            f"**Routing reason:** {reason}\n\n"
            f"### Root cause\n{d.root_cause}\n\n"
            f"### Reasoning\n{d.reasoning}\n\n"
            f"### Proposed confirmation test\n```\n{d.proposed_test}\n```\n\n"
            f"Reply `@agent: fix` to have Kronos attempt an automated fix, "
            f"or `@agent: ignore` to dismiss."
        )


# Imported here to avoid circular import at module top.
from kronos.agent.store import IncidentStore  # noqa: E402
