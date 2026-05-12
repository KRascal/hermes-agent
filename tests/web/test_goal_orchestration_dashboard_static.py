"""Static regression checks for dashboard goal orchestration visibility."""

from pathlib import Path


def test_sessions_page_renders_goal_orchestration_status_card():
    text = Path("web/src/pages/SessionsPage.tsx").read_text()

    assert "goal_orchestration" in text
    assert "Goal-Versioned Single Writer" in text
    assert "writer locked" in text
    assert "stale runs" in text
    assert "current goal" in text
