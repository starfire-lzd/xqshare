"""
Tailscale tunnel process management for xqshare.

The embedded Tailscale SDK (tsnet) is Go-only, so xqshare uses a small Go
sidecar and manages its lifecycle from Python.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


class TailscaleTunnelError(RuntimeError):
    """Raised when the Tailscale sidecar cannot be started."""


@dataclass
class TailscaleTunnelEndpoint:
    """Local endpoint exposed by a client-side Tailscale tunnel."""

    host: str
    port: int
    status_path: Path
    log_path: Path
    pid: Optional[int] = None
    reused: bool = False


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class TailscaleTunnelConfig:
    mode: str
    hostname: str
    state_dir: Path
    listen_port: int
    target_host: str
    target_port: int
    local_host: str
    local_port: int
    authkey: str = ""
    ephemeral: bool = False
    up_timeout: str = "60s"
    binary: Optional[Path] = None

    @classmethod
    def for_server(cls, host: str, port: int) -> "TailscaleTunnelConfig":
        return cls(
            mode="server",
            hostname=os.environ.get("XQSHARE_TS_HOSTNAME", "xqshare-server"),
            state_dir=Path(os.environ.get("XQSHARE_TS_STATE_DIR", "tailscale-proxy/tsnet-state")),
            listen_port=env_int("XQSHARE_TS_LISTEN_PORT", port),
            target_host=os.environ.get("XQSHARE_TS_TARGET_HOST", host),
            target_port=env_int("XQSHARE_TS_TARGET_PORT", port),
            local_host=os.environ.get("XQSHARE_TS_LOCAL_HOST", "127.0.0.1"),
            local_port=env_int("XQSHARE_TS_LOCAL_PORT", port),
            authkey=os.environ.get("XQSHARE_TS_AUTHKEY", os.environ.get("TS_AUTHKEY", "")),
            ephemeral=env_bool("XQSHARE_TS_EPHEMERAL", False),
            up_timeout=os.environ.get("XQSHARE_TS_UP_TIMEOUT", "60s"),
            binary=_env_binary(),
        )

    @classmethod
    def for_client(cls, remote_host: str, remote_port: int) -> "TailscaleTunnelConfig":
        default_hostname = f"xqshare-client-{platform.node() or 'local'}"
        return cls(
            mode="client",
            hostname=os.environ.get("XQSHARE_TS_HOSTNAME", default_hostname),
            state_dir=Path(os.environ.get("XQSHARE_TS_STATE_DIR", str(_default_client_state_dir()))),
            listen_port=env_int("XQSHARE_TS_LISTEN_PORT", remote_port),
            target_host=os.environ.get("XQSHARE_TS_TARGET_HOST", remote_host),
            target_port=env_int("XQSHARE_TS_TARGET_PORT", remote_port),
            local_host=os.environ.get("XQSHARE_TS_LOCAL_HOST", "127.0.0.1"),
            local_port=env_int("XQSHARE_TS_LOCAL_PORT", remote_port),
            authkey=os.environ.get("XQSHARE_TS_AUTHKEY", os.environ.get("TS_AUTHKEY", "")),
            ephemeral=env_bool("XQSHARE_TS_EPHEMERAL", False),
            up_timeout=os.environ.get("XQSHARE_TS_UP_TIMEOUT", "60s"),
            binary=_env_binary(),
        )


class TailscaleTunnel:
    def __init__(self, config: TailscaleTunnelConfig, wait_timeout: Optional[float] = None):
        self.config = config
        self.wait_timeout = wait_timeout if wait_timeout is not None else env_int("XQSHARE_TS_READY_TIMEOUT", 300)
        self.process: Optional[subprocess.Popen] = None
        self.log_path = _log_path(config.mode)

    def start(self, detached: bool = False) -> "TailscaleTunnel":
        binary = self.config.binary or find_sidecar_binary()
        if binary is None:
            binary = build_sidecar_binary()

        state_dir = self.config.state_dir.expanduser()
        if not state_dir.is_absolute():
            state_dir = (Path.cwd() / state_dir).resolve()
        self.config.state_dir = state_dir
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")

        env = os.environ.copy()
        if self.config.authkey:
            env["TS_AUTHKEY"] = self.config.authkey
        env["XQSHARE_TS_AUTHKEY"] = ""

        args = [
            str(binary),
            "-mode",
            self.config.mode,
            "-hostname",
            self.config.hostname,
            "-state-dir",
            str(self.config.state_dir),
            "-listen-port",
            str(self.config.listen_port),
            "-local-host",
            self.config.local_host,
            "-local-port",
            str(self.config.local_port),
            "-target-host",
            self.config.target_host,
            "-target-port",
            str(self.config.target_port),
            "-up-timeout",
            self.config.up_timeout,
        ]
        if self.config.ephemeral:
            args.append("-ephemeral")

        log_file = self.log_path.open("a", encoding="utf-8")
        popen_kwargs: Dict[str, Any] = {
            "stdout": log_file,
            "stderr": log_file,
            "cwd": str(binary.parent),
            "env": env,
            "text": True,
        }
        if detached:
            popen_kwargs["stdin"] = subprocess.DEVNULL
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                popen_kwargs["start_new_session"] = True

        self.process = subprocess.Popen(
            args,
            **popen_kwargs,
        )
        if not detached:
            atexit.register(self.stop)

        self._wait_until_ready()
        return self

    def stop(self) -> None:
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def _wait_until_ready(self) -> None:
        assert self.process is not None
        deadline = time.time() + self.wait_timeout
        login_url_printed = False
        ready_text = (
            "xqshare tailscale client proxy ready"
            if self.config.mode == "client"
            else "xqshare tailscale proxy ready"
        )

        while time.time() < deadline:
            if self.process.poll() is not None:
                raise TailscaleTunnelError(
                    "Tailscale tunnel exited early.\n"
                    f"Log: {self.log_path}\n"
                    f"{self._tail_log()}"
                )
            if self.log_path.exists():
                content = self.log_path.read_text(encoding="utf-8", errors="replace")
                if ready_text in content:
                    return
                login_match = re.search(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+", content)
                if login_match and not login_url_printed:
                    print("")
                    print("Tailscale authorization required. Open this URL in your browser:")
                    print(login_match.group(0))
                    print("")
                    login_url_printed = True
                if "tailscale up failed" in content or "invalid mode" in content:
                    raise TailscaleTunnelError(
                        "Tailscale tunnel failed.\n"
                        f"Log: {self.log_path}\n"
                        f"{self._tail_log()}"
                    )
            time.sleep(0.25)

        raise TailscaleTunnelError(
            f"Tailscale tunnel did not become ready in {self.wait_timeout:.0f}s; "
            f"see log: {self.log_path}\n"
            f"{self._tail_log()}"
        )

    def _tail_log(self, lines: int = 60) -> str:
        if not self.log_path.exists():
            return "<tailscale log does not exist>"
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = content[-lines:]
            if not tail:
                return "<tailscale log is empty>"
            return "\n".join(tail)
        except Exception as exc:
            return f"<failed to read tailscale log: {exc}>"


def start_server_tunnel(host: str, port: int) -> TailscaleTunnel:
    return TailscaleTunnel(TailscaleTunnelConfig.for_server(host, port)).start()


def start_client_tunnel(remote_host: str, remote_port: int) -> TailscaleTunnel:
    return TailscaleTunnel(TailscaleTunnelConfig.for_client(remote_host, remote_port)).start()


def ensure_client_tunnel(remote_host: str, remote_port: int) -> TailscaleTunnelEndpoint:
    """Ensure the client-side Tailscale proxy is running as a shared daemon.

    If the local proxy port is already reachable, this function returns without
    starting a new process. Otherwise it serializes startup with a cross-process
    lock, starts the sidecar detached from the current Python process, and writes
    pid/status metadata for later inspection or shutdown.
    """

    config = TailscaleTunnelConfig.for_client(remote_host, remote_port)
    log_path = _log_path("client")
    status_path = _status_path()

    if _can_connect(config.local_host, config.local_port):
        status = _read_status()
        return TailscaleTunnelEndpoint(
            host=config.local_host,
            port=config.local_port,
            status_path=status_path,
            log_path=Path(status.get("log_path", log_path)),
            pid=status.get("pid"),
            reused=True,
        )

    with _client_tunnel_lock():
        config = TailscaleTunnelConfig.for_client(remote_host, remote_port)
        log_path = _log_path("client")

        if _can_connect(config.local_host, config.local_port):
            status = _read_status()
            return TailscaleTunnelEndpoint(
                host=config.local_host,
                port=config.local_port,
                status_path=status_path,
                log_path=Path(status.get("log_path", log_path)),
                pid=status.get("pid"),
                reused=True,
            )

        _cleanup_stale_status()
        tunnel = TailscaleTunnel(config).start(detached=True)
        pid = tunnel.process.pid if tunnel.process is not None else None
        status = _status_payload(config, tunnel.log_path, pid)
        _write_status(status)

        return TailscaleTunnelEndpoint(
            host=config.local_host,
            port=config.local_port,
            status_path=status_path,
            log_path=tunnel.log_path,
            pid=pid,
            reused=False,
        )


def get_client_tunnel_status() -> Dict[str, Any]:
    """Return status for the shared client-side Tailscale proxy."""

    status = _read_status()
    config = TailscaleTunnelConfig.for_client(
        os.environ.get("XQSHARE_REMOTE_HOST", "localhost"),
        env_int("XQSHARE_REMOTE_PORT", 18812),
    )
    local_host = status.get("local_host", config.local_host)
    local_port = int(status.get("local_port", config.local_port))
    pid = status.get("pid")
    pid_running = _pid_running(pid) if pid else False
    reachable = _can_connect(local_host, local_port)

    result = {
        "running": reachable,
        "reachable": reachable,
        "pid_running": pid_running,
        "pid": pid,
        "local_host": local_host,
        "local_port": local_port,
        "target_host": status.get("target_host", config.target_host),
        "target_port": int(status.get("target_port", config.target_port)),
        "status_path": str(_status_path()),
        "log_path": status.get("log_path", str(_log_path("client"))),
        "state_dir": status.get("state_dir", str(config.state_dir)),
        "started_at": status.get("started_at"),
    }
    if status and not pid_running and not reachable:
        result["stale"] = True
    return result


def stop_client_tunnel() -> bool:
    """Stop the shared client-side Tailscale proxy recorded in the pidfile."""

    stopped = False
    with _client_tunnel_lock():
        status = _read_status()
        pid = status.get("pid")
        if pid and _pid_running(pid):
            _terminate_pid(int(pid))
            stopped = True
        _remove_status()
    return stopped


def _can_connect(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@contextmanager
def _client_tunnel_lock():
    daemon_dir = _daemon_dir()
    daemon_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path()

    if os.name == "nt":
        import msvcrt

        with lock_path.open("a+b") as lock_file:
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _status_payload(config: TailscaleTunnelConfig, log_path: Path, pid: Optional[int]) -> Dict[str, Any]:
    return {
        "pid": pid,
        "mode": config.mode,
        "hostname": config.hostname,
        "local_host": config.local_host,
        "local_port": config.local_port,
        "target_host": config.target_host,
        "target_port": config.target_port,
        "state_dir": str(config.state_dir),
        "log_path": str(log_path),
        "started_at": int(time.time()),
    }


def _read_status() -> Dict[str, Any]:
    path = _status_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_status(status: Dict[str, Any]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    pid = status.get("pid")
    if pid:
        _pid_path().write_text(str(pid), encoding="utf-8")


def _remove_status() -> None:
    for path in (_status_path(), _pid_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _cleanup_stale_status() -> None:
    status = _read_status()
    if not status:
        return
    local_host = status.get("local_host")
    local_port = status.get("local_port")
    pid = status.get("pid")
    reachable = bool(local_host and local_port and _can_connect(str(local_host), int(local_port)))
    if reachable or (pid and _pid_running(pid)):
        return
    _remove_status()


def _pid_running(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int, timeout: float = 5.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_running(pid):
            return
        time.sleep(0.1)

    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass


def _daemon_dir() -> Path:
    value = os.environ.get("XQSHARE_TS_DAEMON_DIR")
    if value:
        return Path(value).expanduser().resolve()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "xqshare" / "tailscale-client"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "xqshare" / "tailscale-client"

    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "xqshare" / "tailscale-client"
    return Path.home() / ".local" / "state" / "xqshare" / "tailscale-client"


def _default_client_state_dir() -> Path:
    return _daemon_dir() / "tsnet-state-client"


def _status_path() -> Path:
    return _daemon_dir() / "client.json"


def _pid_path() -> Path:
    return _daemon_dir() / "client.pid"


def _lock_path() -> Path:
    return _daemon_dir() / "client.lock"


def find_sidecar_binary() -> Optional[Path]:
    candidates = []
    exe_name = "xqshare-tailscale-proxy.exe" if os.name == "nt" else "xqshare-tailscale-proxy"
    platform_name = _platform_binary_name()

    package_root = Path(__file__).resolve().parent.parent
    package_dir = Path(__file__).resolve().parent
    candidates.append(package_dir / "bin" / platform_name)
    candidates.append(package_root / "tailscale-proxy" / "bin" / platform_name)
    candidates.append(package_root / "tailscale-proxy" / exe_name)
    candidates.append(Path.cwd() / "tailscale-proxy" / "bin" / platform_name)
    candidates.append(Path.cwd() / "tailscale-proxy" / exe_name)
    candidates.append(Path.cwd() / exe_name)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def _platform_binary_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        goos = "darwin"
    elif system == "windows":
        goos = "windows"
    elif system == "linux":
        goos = "linux"
    else:
        goos = system

    if machine in {"amd64", "x86_64"}:
        goarch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        goarch = "arm64"
    else:
        goarch = machine

    suffix = ".exe" if goos == "windows" else ""
    return f"xqshare-tailscale-proxy-{goos}-{goarch}{suffix}"


def build_sidecar_binary() -> Path:
    package_root = Path(__file__).resolve().parent.parent
    sidecar_dir = package_root / "tailscale-proxy"
    if not sidecar_dir.exists():
        raise TailscaleTunnelError(
            "tailscale-proxy source directory was not found; install a package "
            "that includes the sidecar binary or set XQSHARE_TS_BINARY"
        )

    go = _find_go()
    if go is None:
        raise TailscaleTunnelError(
            "Go toolchain was not found. Install Go or set XQSHARE_TS_BINARY "
            "to a prebuilt xqshare-tailscale-proxy executable."
        )

    exe_name = "xqshare-tailscale-proxy.exe" if os.name == "nt" else "xqshare-tailscale-proxy"
    output = sidecar_dir / exe_name
    subprocess.run([str(go), "build", "-o", str(output), "."], cwd=sidecar_dir, check=True)
    return output.resolve()


def _find_go() -> Optional[Path]:
    local_go = Path(__file__).resolve().parent.parent / ".tools" / "go" / "bin"
    local_go = local_go / ("go.exe" if os.name == "nt" else "go")
    if local_go.exists():
        return local_go

    from shutil import which

    found = which("go")
    return Path(found) if found else None


def _env_binary() -> Optional[Path]:
    value = os.environ.get("XQSHARE_TS_BINARY")
    return Path(value).expanduser().resolve() if value else None


def _log_path(mode: str) -> Path:
    default_log_dir = _daemon_dir() / "logs" if mode == "client" else Path("logs")
    log_dir = Path(os.environ.get("XQSHARE_LOG_DIR", str(default_log_dir)))
    return log_dir / f"tailscale_{mode}.log"


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Manage the xqshare Tailscale client proxy")
    parser.add_argument(
        "command",
        choices=["start", "status", "stop", "restart"],
        nargs="?",
        default="status",
        help="command to run",
    )
    parser.add_argument("--host", default=None, help="remote tailnet host")
    parser.add_argument("--port", type=int, default=None, help="remote tailnet port")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    remote_host = args.host or os.environ.get("XQSHARE_REMOTE_HOST", "localhost")
    remote_port = args.port or int(os.environ.get("XQSHARE_REMOTE_PORT", "18812"))

    if args.command == "start":
        endpoint = ensure_client_tunnel(remote_host, remote_port)
        status = get_client_tunnel_status()
        status["reused"] = endpoint.reused
        _print_status(status, as_json=args.json)
        return

    if args.command == "status":
        _print_status(get_client_tunnel_status(), as_json=args.json)
        return

    if args.command == "stop":
        stopped = stop_client_tunnel()
        status = get_client_tunnel_status()
        status["stopped"] = stopped
        _print_status(status, as_json=args.json)
        return

    if args.command == "restart":
        stop_client_tunnel()
        endpoint = ensure_client_tunnel(remote_host, remote_port)
        status = get_client_tunnel_status()
        status["reused"] = endpoint.reused
        _print_status(status, as_json=args.json)


def _print_status(status: Dict[str, Any], as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return

    state = "running" if status.get("running") else "stopped"
    print(f"xqshare tailscale client proxy: {state}")
    print(f"  local:  {status.get('local_host')}:{status.get('local_port')}")
    print(f"  target: {status.get('target_host')}:{status.get('target_port')}")
    print(f"  pid:    {status.get('pid') or '-'}")
    print(f"  log:    {status.get('log_path')}")
    print(f"  status: {status.get('status_path')}")
    if status.get("stale"):
        print("  note:   stale status file found")


if __name__ == "__main__":
    main()
