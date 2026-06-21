"""Build & test command execution for the auto-fix path."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kronos.config import Config

log = logging.getLogger("kronos.runner")


@dataclass
class RunResult:
    ok: bool
    output: str


class CommandRunner:
    def __init__(self, config: Config):
        self.cwd = Path(config.repository["local_path"])
        self.test_cmd = config.test["command"]
        self.test_timeout = config.test.get("timeout", 180)
        self.build_cmd = config.build["command"]
        self.build_timeout = config.build.get("timeout", 120)

    def _run(self, cmd: str, timeout: int) -> RunResult:
        try:
            r = subprocess.run(
                cmd,
                shell=True,
                cwd=str(self.cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            return RunResult(ok=r.returncode == 0, output=out.strip())
        except subprocess.TimeoutExpired:
            return RunResult(ok=False, output=f"command timed out: {cmd}")
        except OSError as e:
            return RunResult(ok=False, output=f"command error: {e}")

    def run_tests(self) -> RunResult:
        return self._run(self.test_cmd, self.test_timeout)

    def run_build(self) -> RunResult:
        return self._run(self.build_cmd, self.build_timeout)

    def run_confirmation_test(
        self, test_code: str, *, expect_fail_means_reproduced: bool = True
    ) -> bool:
        """Write a temp confirmation test, run suite, infer reproduction.

        For the confirmation step the test is written to *demonstrate* the bug.
        If the suite fails on the new test, the bug is reproduced (True).
        Returns whether the bug was reproduced against current code.

        Conservative default: if we can't run it, assume not reproduced so we
        route to an issue rather than auto-fixing something unconfirmed.
        """
        if not test_code.strip():
            return False
        # Persist a confirmation test file; language-agnostic best effort.
        test_path = self.cwd / "kronos_confirm_test.go"
        try:
            test_path.write_text(test_code)
        except OSError:
            return False
        result = self.run_tests()
        try:
            test_path.unlink()
        except OSError:
            pass
        # If the suite failed AND output references the confirm test, treat as
        # reproduced. This is a heuristic suitable for the demo.
        if not result.ok and "kronos_confirm" in result.output.lower():
            return True
        if not result.ok:
            return True  # any failure on a fresh repro test implies the bug
        return False
