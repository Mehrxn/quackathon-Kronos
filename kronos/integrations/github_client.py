"""GitHub integration — repo sync, PRs, issues, and approval polling.

Uses PyGithub for the API surface and plain git via subprocess for the
local checkout (clone/fetch/pull, branch, commit). All network-touching
methods degrade gracefully so a demo can run offline against a local repo.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from kronos.config import Config

log = logging.getLogger("kronos.github")

try:
    from github import Github, GithubException  # type: ignore
    _HAS_PYGITHUB = True
except ImportError:  # pragma: no cover
    _HAS_PYGITHUB = False
    Github = None  # type: ignore
    GithubException = Exception  # type: ignore


def _owner_repo(url: str) -> str:
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else url


class GitHubClient:
    def __init__(self, config: Config):
        repo = config.repository
        self.url = repo["github_url"]
        self.token = repo.get("github_token", "")
        self.default_branch = repo.get("default_branch", "main")
        self.local_path = Path(repo["local_path"])
        self.git_timeout = repo.get("git_timeout", 60)
        gh = config.github
        self.reviewers = gh.get("reviewers", [])
        self.pr_labels = gh.get("labels", [])
        self.issue_labels = gh.get("issue_labels", [])
        self.full_name = _owner_repo(self.url)

        self._gh = None
        self._repo = None
        if _HAS_PYGITHUB and self.token:
            try:
                self._gh = Github(self.token)
                self._repo = self._gh.get_repo(self.full_name)
            except GithubException as e:
                log.warning("GitHub API init failed: %s", e)

    # --- local git -----------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.local_path), *args],
            capture_output=True, text=True, timeout=self.git_timeout,
        )

    def sync_repo(self) -> bool:
        """Clone if missing, else fetch + pull if behind origin/default."""
        if not self.local_path.exists():
            self.local_path.parent.mkdir(parents=True, exist_ok=True)
            clone_url = self.url
            if self.token and clone_url.startswith("https://"):
                clone_url = clone_url.replace(
                    "https://", f"https://{self.token}@")
            r = subprocess.run(
                ["git", "clone", clone_url, str(self.local_path)],
                capture_output=True, text=True, timeout=self.git_timeout * 4,
            )
            return r.returncode == 0
        self._git("fetch", "origin")
        local = self._git("rev-parse", "HEAD").stdout.strip()
        remote = self._git("rev-parse", f"origin/{self.default_branch}").stdout.strip()
        if local != remote:
            self._git("checkout", self.default_branch)
            self._git("pull", "origin", self.default_branch)
        return True

    def create_branch(self, name: str) -> bool:
        self._git("checkout", self.default_branch)
        r = self._git("checkout", "-b", name)
        return r.returncode == 0

    def commit_all(self, message: str) -> bool:
        self._git("add", "-A")
        r = self._git("commit", "-m", message)
        return r.returncode == 0

    def push_branch(self, name: str) -> bool:
        r = self._git("push", "-u", "origin", name)
        return r.returncode == 0

    def apply_files(self, files: list[dict]) -> None:
        """Write generated file contents to the working tree."""
        for f in files:
            path = self.local_path / f["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f["content"])

    # --- API: PRs and issues -------------------------------------------------
    def open_pr(self, *, branch: str, title: str, body: str) -> Optional[str]:
        if not self._repo:
            log.info("[dry-run] would open PR %s on branch %s", title, branch)
            return None
        try:
            pr = self._repo.create_pull(
                title=title, body=body, head=branch, base=self.default_branch)
            for lbl in self.pr_labels:
                try:
                    pr.add_to_labels(lbl)
                except GithubException:
                    pass
            if self.reviewers:
                try:
                    pr.create_review_request(reviewers=self.reviewers)
                except GithubException:
                    pass
            return pr.html_url
        except GithubException as e:
            log.error("PR creation failed: %s", e)
            return None

    def open_issue(self, *, title: str, body: str) -> Optional[str]:
        if not self._repo:
            log.info("[dry-run] would open issue %s", title)
            return None
        try:
            issue = self._repo.create_issue(
                title=title, body=body, labels=self.issue_labels)
            return issue.html_url
        except GithubException as e:
            log.error("Issue creation failed: %s", e)
            return None

    def poll_issue_for_command(self, issue_url: str, *,
                               timeout: int, interval: int) -> str:
        """Poll an issue's comments for @agent: fix / @agent: ignore.

        Returns 'fix', 'ignore', or 'timeout'.
        """
        if not self._repo:
            return "timeout"
        m = re.search(r"/issues/(\d+)", issue_url)
        if not m:
            return "timeout"
        number = int(m.group(1))
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            try:
                issue = self._repo.get_issue(number)
                comments = list(issue.get_comments())
                for c in comments[seen:]:
                    text = c.body.lower()
                    if "@agent: fix" in text or "@agent:fix" in text:
                        return "fix"
                    if "@agent: ignore" in text or "@agent:ignore" in text:
                        return "ignore"
                seen = len(comments)
            except GithubException as e:
                log.warning("issue poll error: %s", e)
            time.sleep(interval)
        return "timeout"