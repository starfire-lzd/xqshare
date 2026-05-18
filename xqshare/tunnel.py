"""
Tailscale tunnel process management for xqshare.

The embedded Tailscale SDK (tsnet) is Go-only, so xqshare uses a small Go
sidecar and manages its lifecycle from Python.
"""

from __future__ import annotations

import atexit
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class TailscaleTunnelError(RuntimeError):
    """Raised when the Tailscale sidecar cannot be started."""


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
            state_dir=Path(os.environ.get("XQSHARE_TS_STATE_DIR", "tailscale-proxy/tsnet-state-client")),
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

    def start(self) -> "TailscaleTunnel":
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
        self.process = subprocess.Popen(
            args,
            stdout=log_file,
            stderr=log_file,
            cwd=str(binary.parent),
            env=env,
            text=True,
        )
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
    log_dir = Path(os.environ.get("XQSHARE_LOG_DIR", "logs"))
    return log_dir / f"tailscale_{mode}.log"
