"""TARS Xcode build/test integration."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tars.xcode_manager")


class XcodeError(Exception):
    """Raised on xcodebuild failure."""
    def __init__(self, message: str, log_path: str = ""):
        super().__init__(message)
        self.log_path = log_path


class XcodeManager:
    """Manages Xcode build and test operations."""

    def __init__(
        self,
        workspace: Optional[str] = None,
        project: Optional[str] = None,
        scheme: str = "",
        destination: str = "platform=iOS Simulator,name=iPhone 16",
        derived_data: Optional[str] = None,
    ):
        self.workspace = workspace
        self.project = project
        self.scheme = scheme
        self.destination = destination
        self.derived_data = derived_data

    def _base_cmd(self) -> list[str]:
        """Build the base xcodebuild command."""
        cmd = ["xcodebuild"]

        if self.workspace:
            cmd.extend(["-workspace", self.workspace])
        elif self.project:
            cmd.extend(["-project", self.project])

        if self.scheme:
            cmd.extend(["-scheme", self.scheme])

        cmd.extend(["-destination", self.destination])

        if self.derived_data:
            cmd.extend(["-derivedDataPath", self.derived_data])

        return cmd

    def _run(self, args: list[str], cwd: str, timeout: int = 600) -> subprocess.CompletedProcess:
        """Run xcodebuild with given args."""
        cmd = self._base_cmd() + args
        logger.info("Running: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise XcodeError(f"xcodebuild timed out after {timeout}s")

        return result

    def build(self, cwd: str, configuration: str = "Debug") -> dict:
        """Build the project.

        Returns dict with: success, output, errors
        """
        result = self._run(
            ["build", "-configuration", configuration],
            cwd=cwd,
            timeout=600,
        )

        success = result.returncode == 0
        errors = self._extract_errors(result.stderr + result.stdout)

        if success:
            logger.info("Build succeeded")
        else:
            logger.error("Build failed: %s", errors[:200])

        return {
            "success": success,
            "output": result.stdout,
            "errors": errors,
            "return_code": result.returncode,
        }

    def test(self, cwd: str, configuration: str = "Debug") -> dict:
        """Run tests.

        Returns dict with: success, output, errors, test_results
        """
        result = self._run(
            ["test", "-configuration", configuration],
            cwd=cwd,
            timeout=900,
        )

        success = result.returncode == 0
        errors = self._extract_errors(result.stderr + result.stdout)
        test_results = self._parse_test_results(result.stdout)

        if success:
            logger.info("Tests passed: %s", test_results.get("summary", ""))
        else:
            logger.error("Tests failed: %s", errors[:200])

        return {
            "success": success,
            "output": result.stdout,
            "errors": errors,
            "test_results": test_results,
            "return_code": result.returncode,
        }

    def clean(self, cwd: str) -> bool:
        """Clean derived data."""
        result = self._run(["clean"], cwd=cwd, timeout=120)
        return result.returncode == 0

    def _extract_errors(self, output: str) -> str:
        """Extract error lines from xcodebuild output."""
        error_lines = []
        for line in output.split("\n"):
            line_lower = line.lower()
            if any(kw in line_lower for kw in ["error:", "fatal error", "❌", "failed"]):
                error_lines.append(line.strip())
        return "\n".join(error_lines[-20:])  # Last 20 error lines

    def _parse_test_results(self, output: str) -> dict:
        """Parse test results from xcodebuild output."""
        results = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "summary": "",
            "failures": [],
        }

        for line in output.split("\n"):
            if "Test Suite" in line and "passed" in line:
                results["summary"] = line.strip()
            elif "Test Case" in line:
                if "passed" in line:
                    results["passed"] += 1
                elif "failed" in line:
                    results["failed"] += 1
                    results["failures"].append(line.strip())

        return results

    @classmethod
    def from_config(cls, config: dict) -> "XcodeManager":
        """Create from a project build config."""
        build = config.get("build", {})
        return cls(
            workspace=build.get("workspace"),
            project=build.get("project"),
            scheme=build.get("scheme", ""),
            destination=build.get("destination", "platform=iOS Simulator,name=iPhone 16"),
            derived_data=build.get("derived_data"),
        )
