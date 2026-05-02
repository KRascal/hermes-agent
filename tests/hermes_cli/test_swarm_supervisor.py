from pathlib import Path

import pytest
import yaml

from hermes_cli.swarm_supervisor import (
    ManagedRole,
    build_supervisor_state,
    build_tmux_command,
    collect_managed_roles,
    managed_roles_from_env,
    run_supervisor_once,
    tmux_session_name,
)


@pytest.fixture()
def supervisor_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    swarms_dir = hermes_home / "swarms"
    swarms_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace" / "aniva"
    workspace.mkdir(parents=True)
    (swarms_dir / "aniva.yaml").write_text(
        yaml.safe_dump(
            {
                "project": "aniva",
                "workspace_path": str(workspace),
                "roles": {
                    "orchestrator": {
                        "profile": "aniva-orchestrator",
                        "launch_command": "hermes -p aniva-orchestrator",
                    },
                    "reviewer": {
                        "profile": "aniva-reviewer",
                        "launch_command": "hermes -p aniva-reviewer",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return {"hermes_home": hermes_home, "workspace": workspace}


def test_managed_roles_from_env_dedupes_and_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_SWARM_SUPERVISOR_MANAGED_ROLES", raising=False)
    assert managed_roles_from_env() == ("orchestrator",)
    assert managed_roles_from_env("orchestrator,reviewer,orchestrator") == ("orchestrator", "reviewer")


def test_collect_managed_roles_defaults_to_orchestrator(supervisor_env):
    roles = collect_managed_roles(hermes_home=supervisor_env["hermes_home"])
    assert len(roles) == 1
    assert roles[0].project == "aniva"
    assert roles[0].role == "orchestrator"
    assert roles[0].profile == "aniva-orchestrator"


def test_tmux_session_name_slugifies_values():
    assert tmux_session_name("ANIVA Project", "orchestrator") == "hermes-swarm-aniva-project-orchestrator"


def test_build_tmux_command_contains_profile_and_workspace(supervisor_env):
    role = ManagedRole(
        project="aniva",
        role="orchestrator",
        profile="aniva-orchestrator",
        workspace_path=str(supervisor_env["workspace"]),
        launch_command="hermes -p aniva-orchestrator",
        session_name="hermes-swarm-aniva-orchestrator",
    )
    command = build_tmux_command(role, hermes_home=supervisor_env["hermes_home"])
    assert "aniva-orchestrator" in command
    assert str(supervisor_env["workspace"]) in command
    assert "TERMINAL_CWD" in command
    assert "supervisor" in command


def test_build_supervisor_state_counts_running_roles():
    roles = [
        ManagedRole("aniva", "orchestrator", "aniva-orchestrator", "/tmp/aniva", "hermes -p aniva-orchestrator", "s1"),
        ManagedRole("celebtwins", "orchestrator", "celebtwins-orchestrator", "/tmp/ct", "hermes -p celebtwins-orchestrator", "s2"),
    ]
    state = build_supervisor_state(
        roles,
        running_sessions={"s1"},
        started_sessions={"s1"},
        interval_seconds=15,
    )
    assert state["managed_roles"] == ["orchestrator"]
    assert state["role_count"] == 2
    assert state["running_roles"] == 1
    assert state["projects"]["aniva"]["roles"]["orchestrator"]["started_now"] is True


def test_run_supervisor_once_starts_missing_tmux_session_and_writes_state(supervisor_env, monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr("hermes_cli.swarm_supervisor.shutil_which_tmux", lambda: "/usr/bin/tmux")

    sessions = set()

    def fake_has_session(name):
        return name in sessions

    class Result:
        returncode = 0
        stderr = ""

    def fake_start_tmux(role, hermes_home=None):
        calls.append((role.project, role.role, role.session_name))
        sessions.add(role.session_name)
        return Result()

    monkeypatch.setattr("hermes_cli.swarm_supervisor.tmux_has_session", fake_has_session)
    monkeypatch.setattr("hermes_cli.swarm_supervisor.start_tmux_session", fake_start_tmux)
    monkeypatch.setattr("hermes_cli.swarm_supervisor.tmux_pane_pid", lambda session_name: 4321)

    state = run_supervisor_once(interval_seconds=12, hermes_home=supervisor_env["hermes_home"])

    assert calls == [("aniva", "orchestrator", "hermes-swarm-aniva-orchestrator")]
    assert state["state"] == "running"
    assert state["running_roles"] == 1
    state_path = supervisor_env["hermes_home"] / "state" / "swarm_supervisor_state.json"
    assert state_path.exists()
