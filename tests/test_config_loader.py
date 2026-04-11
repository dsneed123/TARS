"""Tests for config_loader module."""

import os
import sys
import tempfile
import shutil
import pytest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config_loader import (
    _deep_merge,
    load_yaml,
    load_project,
    list_projects,
    load_token_budget,
    load_queue,
)


def test_deep_merge_basic():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    override = {"b": {"c": 99}, "e": 5}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}


def test_deep_merge_no_mutation():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    result = _deep_merge(base, override)
    assert "c" not in base["a"]


def test_load_yaml_missing_file(tmp_path):
    result = load_yaml(tmp_path / "nonexistent.yaml")
    assert result == {}


def test_load_yaml_valid(tmp_path):
    f = tmp_path / "test.yaml"
    f.write_text("key: value\nnested:\n  a: 1\n")
    result = load_yaml(f)
    assert result == {"key": "value", "nested": {"a": 1}}


def test_load_token_budget_defaults():
    budget = load_token_budget()
    assert "daily_limit" in budget
    assert budget["daily_limit"] > 0


def test_load_queue_empty():
    tasks = load_queue()
    assert isinstance(tasks, list)
