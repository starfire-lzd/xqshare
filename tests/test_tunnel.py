"""
Tests for Tailscale tunnel daemon management.
"""

from unittest.mock import patch

import pytest

from pathlib import Path

from xqshare.tunnel import (
    TailscaleTunnel,
    TailscaleTunnelEndpoint,
    ensure_client_tunnel,
    get_client_tunnel_status,
    stop_client_tunnel,
)


@pytest.fixture
def daemon_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XQSHARE_TS_DAEMON_DIR", str(tmp_path / "daemon"))
    monkeypatch.setenv("XQSHARE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("XQSHARE_TS_STATE_DIR", raising=False)
    monkeypatch.delenv("XQSHARE_TS_LOCAL_HOST", raising=False)
    monkeypatch.delenv("XQSHARE_TS_LOCAL_PORT", raising=False)
    monkeypatch.delenv("XQSHARE_TS_TARGET_HOST", raising=False)
    monkeypatch.delenv("XQSHARE_TS_TARGET_PORT", raising=False)
    return tmp_path


class FakeProcess:
    def __init__(self, pid=4321):
        self.pid = pid

    def poll(self):
        return None


def test_ensure_client_tunnel_reuses_reachable_local_proxy(daemon_env):
    with patch("xqshare.tunnel._can_connect", return_value=True), \
            patch("xqshare.tunnel.subprocess.Popen") as mock_popen:
        endpoint = ensure_client_tunnel("xqshare-server", 18812)

    assert endpoint.host == "127.0.0.1"
    assert endpoint.port == 18812
    assert endpoint.reused is True
    mock_popen.assert_not_called()


def test_ensure_client_tunnel_starts_detached_and_writes_status(daemon_env, tmp_path):
    binary = tmp_path / "xqshare-tailscale-proxy"
    binary.write_text("fake", encoding="utf-8")

    with patch("xqshare.tunnel._can_connect", return_value=False), \
            patch("xqshare.tunnel.find_sidecar_binary", return_value=binary), \
            patch.object(TailscaleTunnel, "_wait_until_ready", return_value=None), \
            patch("xqshare.tunnel.subprocess.Popen", return_value=FakeProcess()) as mock_popen:
        endpoint = ensure_client_tunnel("xqshare-server", 18812)
        status = get_client_tunnel_status()

    assert endpoint.pid == 4321
    assert endpoint.reused is False
    assert status["pid"] == 4321
    assert status["target_host"] == "xqshare-server"
    assert status["target_port"] == 18812
    assert mock_popen.call_count == 1
    assert mock_popen.call_args.kwargs.get("start_new_session") is True


def test_ensure_client_tunnel_cleans_stale_status_before_restart(daemon_env, tmp_path):
    status_path = daemon_env / "daemon" / "client.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        '{"pid": 999999, "local_host": "127.0.0.1", "local_port": 18812}',
        encoding="utf-8",
    )
    binary = tmp_path / "xqshare-tailscale-proxy"
    binary.write_text("fake", encoding="utf-8")

    with patch("xqshare.tunnel._can_connect", return_value=False), \
            patch("xqshare.tunnel.find_sidecar_binary", return_value=binary), \
            patch.object(TailscaleTunnel, "_wait_until_ready", return_value=None), \
            patch("xqshare.tunnel.subprocess.Popen", return_value=FakeProcess(pid=1234)):
        endpoint = ensure_client_tunnel("xqshare-server", 18812)

    assert endpoint.pid == 1234


def test_stop_client_tunnel_terminates_recorded_process(daemon_env):
    status_path = daemon_env / "daemon" / "client.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text('{"pid": 4321}', encoding="utf-8")

    with patch("xqshare.tunnel._pid_running", return_value=True), \
            patch("xqshare.tunnel._terminate_pid") as mock_terminate:
        stopped = stop_client_tunnel()

    assert stopped is True
    mock_terminate.assert_called_once_with(4321)
    assert not status_path.exists()


def test_xtquant_remote_uses_daemon_tunnel_without_stopping_it(monkeypatch):
    from xqshare.client import XtQuantRemote

    mock_conn = MockConnection()
    endpoint = TailscaleTunnelEndpoint(
        host="127.0.0.1",
        port=18812,
        status_path=Path("/tmp/status.json"),
        log_path=Path("/tmp/tailscale_client.log"),
        pid=1234,
        reused=True,
    )

    with patch("xqshare.client.rpyc.connect", return_value=mock_conn), \
            patch("xqshare.client.BgServingThread", return_value=MockBgThread()), \
            patch("xqshare.tunnel.ensure_client_tunnel", return_value=endpoint) as mock_ensure:
        client = XtQuantRemote(host="xqshare-server", port=18812, use_tailscale=True, auto_reconnect=False)
        client.close()

    mock_ensure.assert_called_once_with("xqshare-server", 18812)
    assert client._tunnel is None


def test_xtquant_remote_legacy_tunnel_stops_on_close(monkeypatch):
    from xqshare.client import XtQuantRemote

    mock_conn = MockConnection()
    mock_tunnel = MockTunnel()
    monkeypatch.setenv("XQSHARE_TS_DAEMON", "0")

    with patch("xqshare.client.rpyc.connect", return_value=mock_conn), \
            patch("xqshare.client.BgServingThread", return_value=MockBgThread()), \
            patch("xqshare.tunnel.start_client_tunnel", return_value=mock_tunnel) as mock_start:
        client = XtQuantRemote(host="xqshare-server", port=18812, use_tailscale=True, auto_reconnect=False)
        client.close()

    mock_start.assert_called_once_with("xqshare-server", 18812)
    assert mock_tunnel.stopped is True


class MockConnection:
    def __init__(self):
        self.root = MockRoot()

    def close(self):
        pass


class MockRoot:
    def ping(self):
        return "pong"

    def authenticate(self, client_id, client_secret):
        return {"level": "standard"}


class MockTunnel:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class MockBgThread:
    def stop(self):
        pass
