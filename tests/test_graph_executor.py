"""Tests for the node-graph executor's deterministic parts and failure paths."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.graph_executor import (  # noqa: E402
    GraphRun, _changed_files, _syntax_check, load_graph,
)
from lib.llm import extract_json, parse_text_tool_calls  # noqa: E402


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _mk_run(repo: Path, graph_overrides: dict | None = None) -> GraphRun:
    graph = load_graph({"graph": graph_overrides or {}})
    run = GraphRun(
        "testproj", {"id": "t-1", "title": "t", "description": "d"},
        graph, str(repo),
        {"repo": "x/y", "git": {"base_branch": "main", "strategy": "branch-pr"},
         "build": {}, "test": {}},
    )
    return run


def test_changed_files_excludes_vendored(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.js").write_text("syntax error here(")
    (repo / "new.py").write_text("y = 2\n")
    files = _changed_files(str(repo), "main")
    assert "new.py" in files
    assert all("node_modules" not in f for f in files)


def test_syntax_check_scoped(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "bad.py").write_text("def broken(:\n")
    ok, detail = _syntax_check(str(repo), ["bad.py"])
    assert not ok and "bad.py" in detail
    ok, _ = _syntax_check(str(repo), ["app.py"])
    assert ok
    # a bad file NOT in scope must not fail the check
    ok, _ = _syntax_check(str(repo), [])
    assert ok


def test_skip_logic(tmp_path):
    repo = _mk_repo(tmp_path)
    run = _mk_run(repo)
    run.artifacts["intake"] = {"complexity": "trivial", "docs_only": False}
    assert run._should_skip({"skip_for": ["trivial", "docs_only"]})
    assert not run._should_skip({"skip_for": ["docs_only"]})
    run.artifacts["intake"] = {"complexity": "standard", "docs_only": True}
    assert run._should_skip({"skip_for": ["docs_only"]})
    assert not run._should_skip({})


def test_verify_pass_and_state_file(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr("lib.graph_executor.RUNS_DIR", tmp_path / "runs")
    run = _mk_run(repo)
    (repo / "feature.py").write_text("z = 3\n")
    run.node_verify("verify", {}, __import__("time").time())
    assert run.nodes["verify"]["status"] == "ok"
    doc = json.loads((tmp_path / "runs" / "t-1.json").read_text())
    assert doc["nodes"]["verify"]["status"] == "ok"
    assert run.artifacts["verify"]["ok"]


def test_verify_failure_exhausts_fix_loop_and_escalation(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr("lib.graph_executor.RUNS_DIR", tmp_path / "runs")
    run = _mk_run(repo)
    (repo / "broken.py").write_text("def nope(:\n")

    attempts = []

    def fake_agent(model, prompt, system, cwd, max_turns=15, timeout=600):
        attempts.append(model)   # never actually fixes the file
        return {"result": "tried", "calls": 1, "tokens_in": 10, "tokens_out": 5,
                "changed_files": [], "stop_reason": "finished", "is_error": False}

    monkeypatch.setattr(run.llm, "agent", fake_agent)
    run.node_verify("verify", {"max_fix_attempts": 2,
                               "fix_model": "coder",
                               "escalation_model": "escalation"},
                    __import__("time").time())
    assert run.nodes["verify"]["status"] == "failed"
    assert attempts == ["coder", "coder", "escalation"]


def test_verify_empty_diff_fails(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr("lib.graph_executor.RUNS_DIR", tmp_path / "runs")
    run = _mk_run(repo)
    run.node_verify("verify", {}, __import__("time").time())
    assert run.nodes["verify"]["status"] == "failed"
    assert "no changes" in run.nodes["verify"]["error"]


def test_extract_json():
    assert extract_json('noise {"a": 1} trailing')["a"] == 1
    assert extract_json('<think>hmm</think>{"b": [1,2]}')["b"] == [1, 2]
    assert extract_json("no json here") is None


def test_parse_text_tool_calls_variants():
    xml = ('<function=write_file>\n<parameter=path>\na.py\n</parameter>\n'
           '<parameter=content>\nprint("}")\n</parameter>\n</function>')
    calls = parse_text_tool_calls(xml)
    assert calls[0]["function"]["name"] == "write_file"
    assert calls[0]["function"]["arguments"]["content"] == 'print("}")'
    assert parse_text_tool_calls("plain text, no calls") == []


def test_graph_deadlock_detected(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr("lib.graph_executor.RUNS_DIR", tmp_path / "runs")
    run = _mk_run(repo, {"nodes": {
        "a": {"kind": "intake", "needs": ["zzz-not-a-node"]},
    }})
    run.graph["nodes"] = {"a": {"kind": "intake", "needs": ["zzz"]}}
    assert run.run() is False
    doc = json.loads((tmp_path / "runs" / "t-1.json").read_text())
    assert "deadlock" in (doc.get("error") or "")
