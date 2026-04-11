"""Tests for error_analyzer module."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_circuit_breaker_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    (tmp_path / "state").mkdir()

    from lib.error_analyzer import ErrorAnalyzer

    ea = ErrorAnalyzer()

    # Initially not locked
    assert not ea.is_locked("test-project")

    # Record failures below threshold
    ea.record_failure("test-project", "task-1", "error 1")
    ea.record_failure("test-project", "task-2", "error 2")
    assert not ea.is_locked("test-project")

    # Third failure trips the breaker
    ea.record_failure("test-project", "task-3", "error 3")
    assert ea.is_locked("test-project")

    status = ea.get_status("test-project")
    assert status["consecutive_failures"] == 3
    assert status["is_locked"]


def test_reset_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    (tmp_path / "state").mkdir()

    from lib.error_analyzer import ErrorAnalyzer

    ea = ErrorAnalyzer()
    ea.record_failure("proj", "t1")
    ea.record_failure("proj", "t2")
    ea.reset_failures("proj")

    status = ea.get_status("proj")
    assert status["consecutive_failures"] == 0


def test_classify_error():
    from lib.error_analyzer import ErrorAnalyzer

    ea = ErrorAnalyzer()

    result = ea.classify_error("SyntaxError: unexpected token")
    assert result["category"] == "syntax"
    assert result["retryable"]

    result = ea.classify_error("ModuleNotFoundError: No module named 'foo'")
    assert result["category"] == "dependency"

    result = ea.classify_error("Build failed with exit code 1")
    assert result["category"] == "build"

    result = ea.classify_error("AssertionError: expected 5 got 3")
    assert result["category"] == "test"
