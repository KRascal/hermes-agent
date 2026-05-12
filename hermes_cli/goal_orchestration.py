"""Goal-versioned single-writer orchestration state.

This module gives `/goal` a durable workspace-level coordination surface so
newer user instructions do not get overwritten by older autonomous runs.

It intentionally stays small and filesystem-backed:
- `manifest.json` is the source of truth for current goal_version.
- `runs/*.json` records each worker/run and the goal_version it saw.
- `locks.json` allows exactly one active writer per scope while read-only
  scouts/reviewers can overlap.

The agent still reasons normally, but every continuation prompt can now include
an executable guard: compare your run's goal_version with the manifest before
commit/build/deploy and stop if stale.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
DEFAULT_RUN_TTL_SECONDS = 6 * 60 * 60
_STATE_SUBDIR = Path("state") / "goal_orchestration"
_SLUG_RE = re.compile(r"[^a-z0-9_.-]+")
_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class GoalOrchestrationError(RuntimeError):
    """Base error for orchestration state failures."""


class RunConflictError(GoalOrchestrationError):
    """Raised when a writer run conflicts with an active writer lock."""


def _now() -> float:
    return time.time()


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n")
    os.replace(tmp, path)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def state_root() -> Path:
    """Return the root directory for goal orchestration state."""

    return _hermes_home() / _STATE_SUBDIR


def _clean_slug(value: str, *, max_length: int = 48) -> str:
    value = value.strip().lower().replace(" ", "-")
    value = _SLUG_RE.sub("-", value).strip("-._")
    return value[:max_length] or "workspace"


def scope_for_cwd(cwd: str | os.PathLike[str] | None = None) -> str:
    """Derive a stable scope slug from a workspace/repo path."""

    raw = cwd or os.environ.get("TERMINAL_CWD") or os.getcwd()
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    label = _clean_slug(resolved.name or "workspace", max_length=53)
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
    return f"{label}-{digest}"


def scope_dir(scope: str) -> Path:
    if not _SCOPE_RE.fullmatch(scope or ""):
        raise ValueError(f"invalid scope: {scope!r}")
    return state_root() / scope


def _validate_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        raise ValueError(
            "invalid run_id: use 1-80 letters, numbers, dots, underscores, or hyphens; "
            "path separators are not allowed"
        )
    return run_id


@contextmanager
def _locked_scope(scope: str) -> Iterator[Path]:
    directory = scope_dir(scope)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield directory
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise GoalOrchestrationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GoalOrchestrationError(f"expected JSON object in {path}")
    return data


def read_manifest(scope: str) -> dict[str, Any]:
    return _load_json(scope_dir(scope) / "manifest.json")


def _render_goal_md(manifest: dict[str, Any]) -> str:
    return (
        f"# Goal Manifest — {manifest['scope']}\n\n"
        f"- Goal version: {manifest['goal_version']}\n"
        f"- Supersedes version: {manifest.get('supersedes_version')}\n"
        f"- Session: `{manifest.get('session_id')}`\n"
        f"- Repo/CWD: `{manifest.get('repo_path')}`\n"
        f"- Updated: {manifest.get('updated_at')}\n\n"
        "## Current goal\n\n"
        f"{manifest.get('current_goal', '').strip()}\n\n"
        "## Non-regression rule\n\n"
        "Previous acceptance criteria remain active unless this manifest explicitly supersedes them.\n"
    )


def _render_acceptance_md(manifest: dict[str, Any]) -> str:
    return (
        f"# Acceptance / Merge Gate — {manifest['scope']}\n\n"
        "## Mandatory policy\n\n"
        "1. Single Writer: only the active writer run may change canonical files/branches.\n"
        "2. Parallel agents may scout, review, or test, but write-heavy work must use isolated worktrees/branches.\n"
        "3. Before commit, build, push, deploy, or completion report, compare the run goal_version with `manifest.json`.\n"
        "4. If the run is stale, stop; preserve findings/artifacts; do not write back to canonical state.\n"
        "5. Merge gate must verify latest goal, non-regression, tests/lint/typecheck as appropriate, and clean diff.\n\n"
        f"Current goal version: {manifest['goal_version']}\n"
    )


def sync_goal_manifest(
    goal: str,
    *,
    session_id: str,
    cwd: str | os.PathLike[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update the workspace goal manifest and increment its version."""

    goal = (goal or "").strip()
    if not goal:
        raise ValueError("goal text is empty")
    cwd_path = Path(cwd or os.environ.get("TERMINAL_CWD") or os.getcwd()).expanduser()
    scope = scope_for_cwd(cwd_path)
    with _locked_scope(scope) as directory:
        now = _now()
        previous = _load_json(directory / "manifest.json")
        previous_version = int(previous.get("goal_version", 0) or 0)
        version = previous_version + 1
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "scope": scope,
            "repo_path": str(cwd_path),
            "session_id": session_id,
            "current_goal": goal,
            "goal_version": version,
            "supersedes_version": previous_version or None,
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
            "acceptance_criteria": list(acceptance_criteria or previous.get("acceptance_criteria") or []),
            "policy": {
                "single_writer": True,
                "stale_run_must_stop": True,
                "parallel_writes_forbidden": True,
                "merge_gate_required": True,
            },
            "active_writer_run_id": None,
        }
        # A newer goal supersedes any older active writer. Preserve the run as
        # stale for audit, but release the lock so the new goal can proceed.
        if previous_version:
            _mark_superseded_active_writer_stale(directory, current_goal_version=version, now=now)
        # Keep a small audit trail of superseded manifests.
        if previous:
            archive = directory / "versions" / f"v{previous_version}.json"
            _atomic_write_json(archive, previous)
        _atomic_write_json(directory / "manifest.json", manifest)
        _atomic_write_text(directory / "goal.md", _render_goal_md(manifest))
        _atomic_write_text(directory / "acceptance.md", _render_acceptance_md(manifest))
        decisions = directory / "decisions.log"
        decisions.parent.mkdir(parents=True, exist_ok=True)
        with decisions.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), "event": "goal_updated", "goal_version": version, "session_id": session_id}, ensure_ascii=False) + "\n")
        return manifest


def _runs_dir(directory: Path) -> Path:
    path = directory / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_run(directory: Path, run_id: str) -> dict[str, Any] | None:
    run_id = _validate_run_id(run_id)
    path = _runs_dir(directory) / f"{run_id}.json"
    if not path.exists():
        return None
    return _load_json(path)


def _write_run(directory: Path, run: dict[str, Any]) -> None:
    run["run_id"] = _validate_run_id(str(run["run_id"]))
    _atomic_write_json(_runs_dir(directory) / f"{run['run_id']}.json", run)


def _locks_path(directory: Path) -> Path:
    return directory / "locks.json"


def _active_writer(directory: Path, *, now: float) -> dict[str, Any] | None:
    locks = _load_json(_locks_path(directory), {"active_writer_run_id": None})
    run_id = locks.get("active_writer_run_id")
    if not run_id:
        return None
    run = _read_run(directory, str(run_id))
    if not run or run.get("status") != "active" or run.get("role") != "writer":
        return None
    if float(run.get("expires_at", 0) or 0) <= now:
        return None
    return run


def _release_writer_lock(directory: Path, run_id: str | None, *, now: float) -> None:
    locks = _load_json(_locks_path(directory), {"active_writer_run_id": None, "updated_at": None})
    if run_id is None or locks.get("active_writer_run_id") == run_id:
        locks["active_writer_run_id"] = None
        locks["updated_at"] = now
        _atomic_write_json(_locks_path(directory), locks)


def _mark_superseded_active_writer_stale(directory: Path, *, current_goal_version: int, now: float) -> None:
    writer = _active_writer(directory, now=now)
    if not writer:
        return
    if int(writer.get("goal_version", 0) or 0) == current_goal_version:
        return
    writer["status"] = "stale"
    writer["updated_at"] = now
    writer["stale_reason"] = "goal_version_superseded"
    _write_run(directory, writer)
    _release_writer_lock(directory, writer.get("run_id"), now=now)


def register_run(
    scope: str,
    *,
    run_id: str | None = None,
    role: str = "writer",
    paths: list[str] | None = None,
    ttl_seconds: int = DEFAULT_RUN_TTL_SECONDS,
) -> dict[str, Any]:
    """Register a run and acquire the single-writer lock when role=writer."""

    if role not in {"writer", "read-only"}:
        raise ValueError("role must be 'writer' or 'read-only'")
    run_id = _validate_run_id(f"run-{uuid.uuid4().hex[:12]}" if run_id is None else run_id)
    now = _now()
    with _locked_scope(scope) as directory:
        manifest = _load_json(directory / "manifest.json")
        if not manifest:
            raise GoalOrchestrationError(f"no manifest for scope {scope!r}")
        existing = _read_run(directory, run_id)
        if existing and existing.get("status") == "active" and existing.get("role") != role:
            raise RunConflictError(
                f"run_id {run_id} is already active as {existing.get('role')} in scope {scope}"
            )
        if role == "writer":
            current_writer = _active_writer(directory, now=now)
            if current_writer and current_writer.get("run_id") != run_id:
                raise RunConflictError(
                    f"scope {scope} already has active writer {current_writer.get('run_id')} "
                    f"for goal_version {current_writer.get('goal_version')}"
                )
        run = {
            "run_id": run_id,
            "scope": scope,
            "role": role,
            "status": "active",
            "goal_version": int(manifest.get("goal_version", 0) or 0),
            "paths": list(paths or []),
            "created_at": now,
            "updated_at": now,
            "expires_at": now + int(ttl_seconds),
        }
        _write_run(directory, run)
        locks = _load_json(_locks_path(directory), {"active_writer_run_id": None, "updated_at": None})
        if role == "writer":
            locks["active_writer_run_id"] = run_id
            locks["updated_at"] = now
            _atomic_write_json(_locks_path(directory), locks)
            manifest["active_writer_run_id"] = run_id
            manifest["updated_at"] = now
            _atomic_write_json(directory / "manifest.json", manifest)
        return run


def check_run_current(scope: str, run_id: str) -> dict[str, Any]:
    """Return whether a writer run is still allowed to write for the current goal."""

    run_id = _validate_run_id(run_id)
    now = _now()
    with _locked_scope(scope) as directory:
        manifest = _load_json(directory / "manifest.json")
        run = _read_run(directory, run_id)
        if not run:
            return {"current": False, "reason": "run_not_found", "scope": scope, "run_id": run_id}
        current_version = int(manifest.get("goal_version", 0) or 0)
        run_version = int(run.get("goal_version", 0) or 0)
        if run_version != current_version:
            if run.get("status") == "active":
                run["status"] = "stale"
                run["updated_at"] = now
                run["stale_reason"] = "goal_version_superseded"
                _write_run(directory, run)
                _release_writer_lock(directory, run_id, now=now)
            return {
                "current": False,
                "reason": "goal_version_superseded",
                "scope": scope,
                "run_id": run_id,
                "run_goal_version": run_version,
                "current_goal_version": current_version,
            }
        if run.get("role") != "writer":
            return {"current": False, "reason": "run_not_writer", "scope": scope, "run_id": run_id}
        locks = _load_json(_locks_path(directory), {"active_writer_run_id": None})
        if locks.get("active_writer_run_id") != run_id:
            return {"current": False, "reason": "writer_lock_not_owned", "scope": scope, "run_id": run_id}
        if run.get("status") != "active":
            return {"current": False, "reason": "run_not_active", "scope": scope, "run_id": run_id}
        if float(run.get("expires_at", 0) or 0) <= now:
            return {"current": False, "reason": "run_expired", "scope": scope, "run_id": run_id}
        return {
            "current": True,
            "reason": "current",
            "scope": scope,
            "run_id": run_id,
            "run_goal_version": run_version,
            "current_goal_version": current_version,
        }


def complete_run(scope: str, run_id: str, *, status: str = "completed") -> dict[str, Any]:
    """Mark a run complete/failed/stale and release writer lock if owned."""

    if status not in {"completed", "failed", "stale", "cancelled"}:
        raise ValueError("invalid run completion status")
    now = _now()
    run_id = _validate_run_id(run_id)
    with _locked_scope(scope) as directory:
        run = _read_run(directory, run_id)
        if not run:
            raise GoalOrchestrationError(f"run not found: {run_id}")
        run["status"] = status
        run["updated_at"] = now
        _write_run(directory, run)
        locks = _load_json(_locks_path(directory), {"active_writer_run_id": None})
        if locks.get("active_writer_run_id") == run_id:
            locks["active_writer_run_id"] = None
            locks["updated_at"] = now
            _atomic_write_json(_locks_path(directory), locks)
            manifest = _load_json(directory / "manifest.json")
            manifest["active_writer_run_id"] = None
            manifest["updated_at"] = now
            _atomic_write_json(directory / "manifest.json", manifest)
        return run


def continuation_guard_text(manifest: dict[str, Any]) -> str:
    """Return prompt text that makes stale-run handling explicit."""

    scope = manifest.get("scope") or "unknown"
    version = manifest.get("goal_version") or 0
    return (
        "\n\n[Goal-Versioned Single Writer Guard]\n"
        f"Scope: {scope}\n"
        f"Goal version: {version}\n"
        "Single Writer policy is active. Parallel scouts/reviewers are allowed, "
        "but only one writer may modify canonical files/branches. Before commit, "
        "build, push, deploy, or completion report, re-read the goal manifest and "
        "confirm this run is not stale. If the manifest goal_version is newer than "
        "this prompt, stop writing, preserve artifacts/findings, and let the current "
        "orchestrator/merge gate decide. Do not overwrite newer work.\n"
    )


def manifest_status_markdown(scope: str) -> str:
    manifest = read_manifest(scope)
    directory = scope_dir(scope)
    locks = _load_json(_locks_path(directory), {"active_writer_run_id": None})
    return (
        f"# Goal orchestration status — {scope}\n\n"
        f"- Goal version: {manifest.get('goal_version')}\n"
        f"- Active writer: {locks.get('active_writer_run_id') or 'none'}\n"
        f"- Goal: {manifest.get('current_goal')}\n"
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal-versioned single-writer orchestration guard")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync-goal")
    p_sync.add_argument("goal")
    p_sync.add_argument("--session-id", required=True)
    p_sync.add_argument("--cwd")

    p_start = sub.add_parser("start-run")
    p_start.add_argument("scope")
    p_start.add_argument("--run-id")
    p_start.add_argument("--role", choices=["writer", "read-only"], default="writer")
    p_start.add_argument("--path", action="append", dest="paths")

    p_check = sub.add_parser("check-run")
    p_check.add_argument("scope")
    p_check.add_argument("run_id")

    p_done = sub.add_parser("complete-run")
    p_done.add_argument("scope")
    p_done.add_argument("run_id")
    p_done.add_argument("--status", default="completed", choices=["completed", "failed", "stale", "cancelled"])

    p_status = sub.add_parser("status")
    p_status.add_argument("scope")
    p_status.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "sync-goal":
            print(json.dumps(sync_goal_manifest(args.goal, session_id=args.session_id, cwd=args.cwd), ensure_ascii=False, indent=2))
        elif args.command == "start-run":
            print(json.dumps(register_run(args.scope, run_id=args.run_id, role=args.role, paths=args.paths), ensure_ascii=False, indent=2))
        elif args.command == "check-run":
            result = check_run_current(args.scope, args.run_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("current") else 2
        elif args.command == "complete-run":
            print(json.dumps(complete_run(args.scope, args.run_id, status=args.status), ensure_ascii=False, indent=2))
        elif args.command == "status":
            if args.json:
                print(json.dumps(read_manifest(args.scope), ensure_ascii=False, indent=2))
            else:
                print(manifest_status_markdown(args.scope))
        return 0
    except RunConflictError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 3
    except (GoalOrchestrationError, ValueError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
