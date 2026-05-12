"""Regression guards for scripts/run_tests.sh goal-version checks."""

from pathlib import Path


def test_run_tests_script_checks_goal_guard_before_and_after_pytest():
    script = Path("scripts/run_tests.sh").read_text()

    assert "HERMES_GOAL_SCOPE" in script
    assert "HERMES_GOAL_RUN_ID" in script
    assert "check-run" in script
    assert "run_goal_guard_check pre-pytest" in script
    assert "run_goal_guard_check post-pytest" in script
    assert "pytest_status" in script
