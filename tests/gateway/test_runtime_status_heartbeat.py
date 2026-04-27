import asyncio

import pytest

from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_runtime_status_heartbeat_refreshes_while_running(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._runtime_status_heartbeat_interval = 30.0

    calls: list[str] = []

    def fake_update(state=None, exit_reason=None):
        calls.append(state)
        if len(calls) >= 2:
            runner._running = False

    async def fake_sleep(_interval):
        return None

    runner._update_runtime_status = fake_update
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await GatewayRunner._runtime_status_heartbeat(runner, interval=1)

    assert calls == ["running", "running"]


@pytest.mark.asyncio
async def test_runtime_status_heartbeat_is_noop_when_not_running():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = False
    runner._runtime_status_heartbeat_interval = 30.0
    runner._update_runtime_status = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run"))

    await GatewayRunner._runtime_status_heartbeat(runner, interval=1)
