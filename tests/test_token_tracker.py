"""Tests for token_tracker module."""

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_token_tracker_record(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    # Create required dirs
    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()

    from lib.token_tracker import TokenTracker

    tt = TokenTracker()
    tt.record(1000, 500, 0.01)

    usage = tt.get_today_usage()
    assert usage["tokens_in"] == 1000
    assert usage["tokens_out"] == 500
    assert usage["total_tokens"] == 1500
    assert usage["requests"] == 1


def test_token_tracker_can_spend(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()

    from lib.token_tracker import TokenTracker

    tt = TokenTracker()
    assert tt.can_spend()


def test_token_tracker_remaining(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    (tmp_path / "state").mkdir()
    (tmp_path / "config").mkdir()

    from lib.token_tracker import TokenTracker

    tt = TokenTracker()
    remaining = tt.get_remaining_budget()
    assert remaining > 0
