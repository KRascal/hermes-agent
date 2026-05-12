import os
import stat
import subprocess

from tools.environments import local as local_env


class FakeProc:
    def __init__(self, pid=12345):
        self.pid = pid
        self.returncode = None
        self.stdin = None

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def _enable_systemd(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LOCAL_SYSTEMD_RUN", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(local_env, "_IS_WINDOWS", False)
    monkeypatch.setattr(local_env.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(local_env.os, "getuid", lambda: 1000)
    monkeypatch.setattr(local_env.os, "getgid", lambda: 1000)


def test_systemd_run_opt_in_uses_transient_service_with_environment_file(monkeypatch, tmp_path):
    _enable_systemd(monkeypatch, tmp_path)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        proc = FakeProc()
        return proc

    monkeypatch.setattr(local_env.subprocess, "Popen", fake_popen)

    proc = local_env._popen_maybe_systemd_run(
        ["/bin/bash", "-c", "printf ok"],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin", "SECRET_TOKEN": "super-secret-token"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        preexec_fn=os.setsid,
        unit_prefix="hermes-tool",
    )

    argv = captured["argv"]
    joined = " ".join(argv)
    assert argv[:3] == ["sudo", "-n", "systemd-run"]
    assert "--collect" in argv
    assert "--wait" in argv
    assert "--pipe" in argv
    assert "--quiet" in argv
    assert any(arg.startswith("--unit=hermes-tool-") for arg in argv)
    assert "--scope" not in argv
    assert not any(arg.startswith("--setenv") for arg in argv)
    assert "super-secret-token" not in joined
    env_props = [arg for arg in argv if arg.startswith("--property=EnvironmentFile=")]
    assert len(env_props) == 1
    env_file = env_props[0].split("=", 2)[2]
    assert os.path.exists(env_file)
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert getattr(proc, "_hermes_systemd_unit").startswith("hermes-tool-")
    assert getattr(proc, "_hermes_systemd_env_file") == env_file
    assert captured["kwargs"]["env"] is None
    assert "preexec_fn" not in captured["kwargs"]

    local_env._cleanup_systemd_run_proc(proc)
    assert not os.path.exists(env_file)


def test_systemd_run_disabled_uses_plain_popen(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_LOCAL_SYSTEMD_RUN", raising=False)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(local_env.subprocess, "Popen", fake_popen)

    local_env._popen_maybe_systemd_run(
        ["/bin/bash", "-c", "printf ok"],
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        preexec_fn=os.setsid,
    )

    assert captured["argv"][:2] == ["/bin/bash", "-c"]
    assert captured["kwargs"]["env"] == {"PATH": "/usr/bin"}
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_systemd_run_unavailable_falls_back_to_plain_popen(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LOCAL_SYSTEMD_RUN", "1")
    monkeypatch.setattr(local_env, "_IS_WINDOWS", False)
    monkeypatch.setattr(local_env.shutil, "which", lambda name: None if name == "systemd-run" else f"/usr/bin/{name}")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(local_env.subprocess, "Popen", fake_popen)

    local_env._popen_maybe_systemd_run(
        ["/bin/bash", "-c", "printf ok"],
        cwd=str(tmp_path),
        env={},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    assert captured["argv"][:2] == ["/bin/bash", "-c"]


def test_systemd_unit_is_killed_on_terminal_timeout_or_interrupt(monkeypatch):
    monkeypatch.setattr(local_env.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(local_env.subprocess, "run", fake_run)

    local_env._kill_systemd_unit("hermes-tool-test.service", escalate=True)

    assert ["sudo", "-n", "systemctl", "kill", "--kill-whom=all", "hermes-tool-test.service"] in calls
    assert ["sudo", "-n", "systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", "hermes-tool-test.service"] in calls
    assert ["sudo", "-n", "systemctl", "stop", "hermes-tool-test.service"] in calls
