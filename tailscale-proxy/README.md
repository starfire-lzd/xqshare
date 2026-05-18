# xqshare Tailscale Proxy

这是一个基于 Tailscale `tsnet` SDK 的 sidecar。它支持两种模式：

- `server`: 在服务端机器上加入 tailnet，并把 tailnet 内的 TCP 连接转发到本机正在运行的 `xqshare-server`。
- `client`: 在客户端机器上加入 tailnet，本地监听 `127.0.0.1:<port>`，再通过 tailnet 拨到服务端。

仓库的 `bin/` 目录包含预编译二进制，Python 会按当前系统自动选择：

- `xqshare-tailscale-proxy-windows-amd64.exe`
- `xqshare-tailscale-proxy-windows-arm64.exe`
- `xqshare-tailscale-proxy-darwin-amd64`
- `xqshare-tailscale-proxy-darwin-arm64`
- `xqshare-tailscale-proxy-linux-amd64`
- `xqshare-tailscale-proxy-linux-arm64`

## 为什么是 sidecar

`xqshare` 是 Python/RPyC 项目，而 Tailscale 官方嵌入式 SDK `tsnet` 是 Go 包。把它做成独立 sidecar 可以复用现有 Python 协议、认证和权限逻辑，同时获得 Tailscale 的 NAT 穿透、WireGuard 加密和 tailnet ACL。

## 服务端启动

先在 Windows 服务端启动 xqshare：

```powershell
python -m xqshare.server --host 127.0.0.1 --port 18812
```

再启动 Tailscale proxy：

```powershell
cd tailscale-proxy
$env:TS_AUTHKEY="tskey-auth-xxxxx"
go run . -hostname xqshare-server -listen-port 18812 -target-host 127.0.0.1 -target-port 18812
```

Python 自动模式：

```powershell
python -m xqshare.server --tailscale --host 127.0.0.1 --port 18812
```

首次接入可以用 auth key；如果不设置 auth key，`tsnet` 会打印登录 URL，按提示在浏览器中授权。生产环境建议使用非 ephemeral 节点，并保留 `tsnet-state` 目录以便重启后复用同一个 Tailscale 身份。

## 客户端访问

客户端需要加入同一个 tailnet，然后把服务端地址配置为 Tailscale MagicDNS 名称或 100.x 地址：

```bash
export XQSHARE_REMOTE_HOST="xqshare-server"
export XQSHARE_REMOTE_PORT="18812"
export XQSHARE_CLIENT_ID="enterprise-user"
export XQSHARE_CLIENT_SECRET="your-secret-here"
```

然后照常使用：

```python
from xqshare import XtQuantRemote

with XtQuantRemote() as xt:
    print(xt.get_service_status())
```

Python 自动客户端模式：

```bash
export XQSHARE_TAILSCALE=1
export XQSHARE_REMOTE_HOST=xqshare-server
export XQSHARE_REMOTE_PORT=18812
python examples/get_stock_list.py --sector "沪深A股"
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TS_AUTHKEY` | 空 | Tailscale auth key，优先级低于 `XQSHARE_TS_AUTHKEY` |
| `XQSHARE_TS_AUTHKEY` | 空 | Tailscale auth key |
| `XQSHARE_TAILSCALE` | `false` | Python 是否自动启动 sidecar |
| `XQSHARE_TS_HOSTNAME` | `xqshare-server` | tailnet 中显示的节点名 |
| `XQSHARE_TS_STATE_DIR` | `./tsnet-state` | tsnet 状态目录 |
| `XQSHARE_TS_LISTEN_PORT` | `18812` | tailnet 监听端口 |
| `XQSHARE_TS_LOCAL_HOST` | `127.0.0.1` | 客户端模式本地监听地址 |
| `XQSHARE_TS_LOCAL_PORT` | `18812` | 客户端模式本地监听端口 |
| `XQSHARE_TS_TARGET_HOST` | `127.0.0.1` | 本机 xqshare-server 地址 |
| `XQSHARE_TS_TARGET_PORT` | `18812` | 本机 xqshare-server 端口 |
| `XQSHARE_TS_EPHEMERAL` | `false` | 是否使用 ephemeral 节点 |
| `XQSHARE_TS_UP_TIMEOUT` | `60s` | 等待 Tailscale 接入成功的最长时间 |

## 安全建议

- 让 `xqshare-server` 只监听 `127.0.0.1`，避免同时暴露到局域网。
- 继续保留 `XQSHARE_CLIENT_ID` / `XQSHARE_CLIENT_SECRET` 权限认证；Tailscale 负责网络层身份和链路加密，xqshare 负责业务层授权。
- 在 Tailscale ACL 中限制哪些用户或设备可以访问 `xqshare-server:18812`。
