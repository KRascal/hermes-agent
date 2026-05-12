"""Tests for goal-versioned single-writer orchestration state."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_sync_goal_manifest_increments_version_and_writes_truth_files(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import read_manifest, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()

    first = sync_goal_manifest("ship the fanclub flow", session_id="sid-1", cwd=repo)
    second = sync_goal_manifest("ship the fanclub flow plus onboarding", session_id="sid-1", cwd=repo)

    assert first["goal_version"] == 1
    assert second["goal_version"] == 2
    assert second["supersedes_version"] == 1
    assert second["policy"]["single_writer"] is True
    assert second["policy"]["stale_run_must_stop"] is True

    manifest = read_manifest(second["scope"])
    assert manifest["current_goal"] == "ship the fanclub flow plus onboarding"

    scope_dir = hermes_home / "state" / "goal_orchestration" / second["scope"]
    assert "ship the fanclub flow plus onboarding" in (scope_dir / "goal.md").read_text()
    assert "Goal version: 2" in (scope_dir / "goal.md").read_text()
    assert "Single Writer" in (scope_dir / "acceptance.md").read_text()


def test_single_writer_rejects_second_writer_for_same_scope(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import (
        RunConflictError,
        register_run,
        sync_goal_manifest,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = sync_goal_manifest("ship it", session_id="sid-1", cwd=repo)

    first = register_run(manifest["scope"], run_id="writer-1", role="writer", paths=["app/**"])
    assert first["role"] == "writer"
    assert first["goal_version"] == 1

    with pytest.raises(RunConflictError) as exc:
        register_run(manifest["scope"], run_id="writer-2", role="writer", paths=["app/**"])

    assert "writer-1" in str(exc.value)


def test_read_only_runs_can_overlap_writer(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = sync_goal_manifest("ship it", session_id="sid-1", cwd=repo)

    register_run(manifest["scope"], run_id="writer-1", role="writer", paths=["app/**"])
    reader = register_run(manifest["scope"], run_id="reviewer-1", role="read-only")

    assert reader["role"] == "read-only"
    assert reader["run_id"] == "reviewer-1"


def test_check_run_current_detects_superseded_goal(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import check_run_current, register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_v1 = sync_goal_manifest("first goal", session_id="sid-1", cwd=repo)
    register_run(manifest_v1["scope"], run_id="writer-1", role="writer")

    assert check_run_current(manifest_v1["scope"], "writer-1")["current"] is True

    sync_goal_manifest("newer goal", session_id="sid-1", cwd=repo)
    status = check_run_current(manifest_v1["scope"], "writer-1")

    assert status["current"] is False
    assert status["reason"] == "goal_version_superseded"
    assert status["run_goal_version"] == 1
    assert status["current_goal_version"] == 2


def test_complete_run_releases_writer_lock(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import complete_run, register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = sync_goal_manifest("ship it", session_id="sid-1", cwd=repo)

    register_run(manifest["scope"], run_id="writer-1", role="writer")
    completed = complete_run(manifest["scope"], "writer-1", status="completed")
    assert completed["status"] == "completed"

    second = register_run(manifest["scope"], run_id="writer-2", role="writer")
    assert second["run_id"] == "writer-2"


def test_check_run_current_rejects_read_only_run_for_write_guard(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import check_run_current, register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = sync_goal_manifest("ship it", session_id="sid-1", cwd=repo)
    register_run(manifest["scope"], run_id="reviewer-1", role="read-only")

    status = check_run_current(manifest["scope"], "reviewer-1")

    assert status["current"] is False
    assert status["reason"] == "run_not_writer"


def test_new_goal_marks_old_writer_stale_and_releases_lock(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import check_run_current, read_manifest, register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest_v1 = sync_goal_manifest("first goal", session_id="sid-1", cwd=repo)
    register_run(manifest_v1["scope"], run_id="writer-1", role="writer")

    sync_goal_manifest("newer goal", session_id="sid-1", cwd=repo)
    stale = check_run_current(manifest_v1["scope"], "writer-1")
    assert stale["current"] is False
    assert stale["reason"] == "goal_version_superseded"

    manifest_v2 = read_manifest(manifest_v1["scope"])
    assert manifest_v2["active_writer_run_id"] is None

    second = register_run(manifest_v1["scope"], run_id="writer-2", role="writer")
    assert second["goal_version"] == 2


def test_register_run_rejects_path_traversal_run_id(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = sync_goal_manifest("ship it", session_id="sid-1", cwd=repo)

    for bad in ("../owned", "/tmp/owned", "bad/slash", ""):
        with pytest.raises(ValueError):
            register_run(manifest["scope"], run_id=bad, role="read-only")


def test_scope_for_cwd_accepts_long_workspace_names(tmp_path):
    from hermes_cli.goal_orchestration import scope_dir, scope_for_cwd

    repo = tmp_path / ("very-long-workspace-name-" * 5)
    repo.mkdir()
    scope = scope_for_cwd(repo)

    assert len(scope) <= 64
    assert scope_dir(scope).name == scope


def test_read_only_cannot_reuse_active_writer_run_id(hermes_home, tmp_path):
    from hermes_cli.goal_orchestration import RunConflictError, check_run_current, register_run, sync_goal_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = sync_goal_manifest("ship it", session_id="sid-1", cwd=repo)
    register_run(manifest["scope"], run_id="same-id", role="writer")

    with pytest.raises(RunConflictError):
        register_run(manifest["scope"], run_id="same-id", role="read-only")

    assert check_run_current(manifest["scope"], "same-id")["current"] is True


def test_goal_manager_sets_manifest_version_and_guard_prompt(hermes_home, tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    mgr = goals.GoalManager(session_id="goal-guard-sid")
    state = mgr.set("finish without regressions")

    assert state.goal_version == 1
    assert state.manifest_scope

    prompt = mgr.next_continuation_prompt()
    assert "Goal version: 1" in prompt
    assert "Single Writer" in prompt
    assert "stale" in prompt.lower()

    raw = (hermes_home / "state" / "goal_orchestration" / state.manifest_scope / "manifest.json").read_text()
    manifest = json.loads(raw)
    assert manifest["current_goal"] == "finish without regressions"
