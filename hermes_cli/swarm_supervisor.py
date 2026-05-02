from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTERVAL_SECONDS = 15
DEFAULT_MANAGED_ROLES = ("orchestrator",)


@dataclass
class ManagedRole:
    project: str
    role: str
    profile: str
    workspace_path: str
    launch_command: str
    session_name: str


def slugify(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "")).strip("-").lower()
    return text or "swarm"


def managed_roles_from_env(value: str | None = None) -> tuple[str, ...]:
    raw = value if value is not None else os.getenv("HERMES_SWARM_SUPERVISOR_MANAGED_ROLES", ",".join(DEFAULT_MANAGED_ROLES))
    roles = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    deduped: list[str] = []
    for role in roles:
        if role not in deduped:
            deduped.append(role)
    return tuple(deduped or DEFAULT_MANAGED_ROLES)


def tmux_session_name(project: str, role: str) -> str:
    return f"hermes-swarm-{slugify(project)}-{slugify(role)}"


def load_swarm_manifests(hermes_home: Path | None = None) -> list[dict[str, Any]]:
    root = Path(hermes_home or get_hermes_home()).expanduser()
    swarms_dir = root / "swarms"
    manifests: list[dict[str, Any]] = []
    if not swarms_dir.exists():
        return manifests
    for manifest_path in sorted(swarms_dir.glob("*.yaml")):
        try:
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload.setdefault("manifest_path", str(manifest_path))
        manifests.append(payload)
    return manifests


def collect_managed_roles(hermes_home: Path | None = None, roles: tuple[str, ...] | None = None) -> list[ManagedRole]:
    managed = roles or managed_roles_from_env()
    items: list[ManagedRole] = []
    for manifest in load_swarm_manifests(hermes_home=hermes_home):
        project = str(manifest.get("project") or Path(str(manifest.get("manifest_path") or "swarm")).stem)
        workspace_path = str(manifest.get("workspace_path") or "")
        role_map = manifest.get("roles") or {}
        if not isinstance(role_map, dict):
            continue
        for role_name in managed:
            role_meta = role_map.get(role_name)
            if not isinstance(role_meta, dict):
                continue
            profile = str(role_meta.get("profile") or "").strip()
            if not profile:
                continue
            launch_command = str(role_meta.get("launch_command") or f"hermes -p {profile}").strip()
            items.append(
                ManagedRole(
                    project=project,
                    role=role_name,
                    profile=profile,
                    workspace_path=workspace_path,
                    launch_command=launch_command,
                    session_name=tmux_session_name(project, role_name),
                )
            )
    return items


def tmux_has_session(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def tmux_pane_pid(session_name: str) -> int | None:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    first = (result.stdout or "").strip().splitlines()
    if not first:
        return None
    try:
        return int(first[0])
    except ValueError:
        return None


def build_tmux_command(role: ManagedRole, *, hermes_home: Path | None = None) -> str:
    hermes_home = Path(hermes_home or get_hermes_home()).expanduser().resolve()
    python_bin = REPO_ROOT / "venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path("python3")
    env_exports = [
        f"export HERMES_HOME={shlex.quote(str(hermes_home))}",
    ]
    if role.workspace_path:
        env_exports.append(f"export TERMINAL_CWD={shlex.quote(role.workspace_path)}")
    command = " ".join([
        shlex.quote(str(python_bin)),
        "-m",
        "hermes_cli.main",
        "-p",
        shlex.quote(role.profile),
        "chat",
        "--source",
        "supervisor",
    ])
    return " && ".join([
        f"cd {shlex.quote(str(REPO_ROOT))}",
        *env_exports,
        f"exec {command}",
    ])


def start_tmux_session(role: ManagedRole, *, hermes_home: Path | None = None) -> subprocess.CompletedProcess:
    cmd = build_tmux_command(role, hermes_home=hermes_home)
    return subprocess.run(
        ["tmux", "new-session", "-d", "-s", role.session_name, cmd],
        capture_output=True,
        text=True,
        check=False,
    )


def build_supervisor_state(
    managed_roles: list[ManagedRole],
    *,
    running_sessions: set[str],
    started_sessions: set[str],
    interval_seconds: int,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    projects: dict[str, Any] = {}
    for role in managed_roles:
        project_meta = projects.setdefault(
            role.project,
            {
                "workspace_path": role.workspace_path,
                "roles": {},
            },
        )
        project_meta["roles"][role.role] = {
            "profile": role.profile,
            "session_name": role.session_name,
            "running": role.session_name in running_sessions,
            "started_now": role.session_name in started_sessions,
            "launch_command": role.launch_command,
            "pane_pid": tmux_pane_pid(role.session_name) if role.session_name in running_sessions else None,
        }
    role_count = sum(len((meta.get("roles") or {})) for meta in projects.values())
    running_count = sum(
        1
        for meta in projects.values()
        for role_meta in (meta.get("roles") or {}).values()
        if isinstance(role_meta, dict) and role_meta.get("running")
    )
    return {
        "state": "running" if not errors else "degraded",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interval_seconds": interval_seconds,
        "managed_roles": list(dict.fromkeys(role.role for role in managed_roles)),
        "projects": projects,
        "role_count": role_count,
        "running_roles": running_count,
        "errors": errors or [],
    }


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = Path(path or (get_hermes_home() / "state" / "swarm_supervisor_state.json")).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_supervisor_once(*, interval_seconds: int = DEFAULT_INTERVAL_SECONDS, hermes_home: Path | None = None) -> dict[str, Any]:
    hermes_home = Path(hermes_home or get_hermes_home()).expanduser()
    roles = collect_managed_roles(hermes_home=hermes_home)
    started_sessions: set[str] = set()
    running_sessions: set[str] = set()
    errors: list[str] = []

    tmux_path = shutil_which_tmux()
    if not tmux_path:
        errors.append("tmux not found")
        state = build_supervisor_state(roles, running_sessions=running_sessions, started_sessions=started_sessions, interval_seconds=interval_seconds, errors=errors)
        write_state(state)
        return state

    for role in roles:
        if tmux_has_session(role.session_name):
            running_sessions.add(role.session_name)
            continue
        result = start_tmux_session(role, hermes_home=hermes_home)
        if result.returncode == 0 and tmux_has_session(role.session_name):
            started_sessions.add(role.session_name)
            running_sessions.add(role.session_name)
        else:
            stderr = (result.stderr or "").strip()
            errors.append(f"{role.project}:{role.role} start failed: {stderr or result.returncode}")

    state = build_supervisor_state(roles, running_sessions=running_sessions, started_sessions=started_sessions, interval_seconds=interval_seconds, errors=errors)
    write_state(state)
    return state


def shutil_which_tmux() -> str | None:
    from shutil import which

    return which("tmux")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Always-on Hermes swarm supervisor")
    parser.add_argument("--once", action="store_true", help="Run a single supervision cycle and exit")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Loop interval in seconds")
    parser.add_argument("--hermes-home", default=str(get_hermes_home()), help="Hermes home path")
    args = parser.parse_args(argv)

    hermes_home = Path(args.hermes_home).expanduser()
    if args.once:
        state = run_supervisor_once(interval_seconds=max(1, args.interval), hermes_home=hermes_home)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state.get("state") == "running" else 1

    while True:
        run_supervisor_once(interval_seconds=max(1, args.interval), hermes_home=hermes_home)
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
