# Tailscale 自动内网穿透

目标：服务端运行 Windows/QMT 长服务；客户端由 Python 按需启动本机 Tailscale sidecar，启动后后台常驻，供多个命令、多个分支、多个程序复用。

## 服务端 Windows

服务端默认运行在 Windows/QMT 机器上。

```powershell
cd D:\develop\workspace\量化交易\xtquant_share\xqshare
$env:TS_AUTHKEY="tskey-auth-xxxxx"
.\scripts\run-server-tailscale.ps1
```

更完整的一键服务端脚本会检查 Python、安装 xqshare、安装 xtquant，并判断首次是否需要 auth key：

```powershell
.\scripts\xqshare-server-bootstrap.ps1 -AuthKey "tskey-auth-xxxxx"
```

已有持久化状态后不需要再传 key：

```powershell
.\scripts\xqshare-server-bootstrap.ps1
```

可选参数都有默认值：

```powershell
.\scripts\xqshare-server-bootstrap.ps1 -HostAddress 127.0.0.1 -Port 18812 -StateDir "$env:LOCALAPPDATA\xqshare\tsnet-server"
```

脚本会执行：

1. `python -m pip install -e .`
2. 使用仓库内预编译的 `tailscale-proxy/bin/xqshare-tailscale-proxy-*`
3. 设置 `XQSHARE_TAILSCALE=1`
4. 启动 `python -m xqshare.server --tailscale --host 127.0.0.1 --port 18812`

成功后，服务端只监听本机 `127.0.0.1:18812`，Tailscale sidecar 在 tailnet 中暴露 `xqshare-server:18812`。

## 客户端 Windows

```powershell
cd D:\develop\workspace\量化交易\xtquant_share\xqshare
$env:TS_AUTHKEY="tskey-auth-xxxxx"  # 首次接入需要；已有 tsnet-state-client 后可省略
$env:XQSHARE_CLIENT_ID="enterprise-user"
$env:XQSHARE_CLIENT_SECRET="your-secret"
$env:XQSHARE_TAILSCALE="1"
$env:XQSHARE_REMOTE_HOST="xqshare-server"
xqshare-tunnel start
```

客户端 sidecar 默认后台常驻，本地监听 `127.0.0.1:18812`，再通过 tailnet 连接 `xqshare-server:18812`。后续命令会直接复用它，不会每个进程再启动一个。

## 客户端 macOS/Linux

```bash
cd /path/to/xqshare
export TS_AUTHKEY="tskey-auth-xxxxx"   # 首次接入需要；已有 tsnet-state-client 后可省略
export XQSHARE_CLIENT_ID="enterprise-user"
export XQSHARE_CLIENT_SECRET="your-secret"
export XQSHARE_TAILSCALE=1
export XQSHARE_REMOTE_HOST=xqshare-server
export XQSHARE_REMOTE_PORT=18812
xqshare-tunnel start
```

## Python API 自动模式

客户端代码不需要手动连 Tailscale，只要设置环境变量：

```bash
export XQSHARE_TAILSCALE=1
export XQSHARE_REMOTE_HOST=xqshare-server
export XQSHARE_REMOTE_PORT=18812
```

然后正常使用：

```python
from xqshare import XtQuantRemote

with XtQuantRemote() as xt:
    print(xt.get_service_status())
```

如果本地代理未运行，第一次创建客户端会自动后台启动；如果已经运行，则直接复用 `127.0.0.1:18812`。

## 命令行工具自动模式

```bash
export XQSHARE_TAILSCALE=1
export XQSHARE_REMOTE_HOST=xqshare-server
xtdata get_stock_list_in_sector --sector-name "沪深A股"
```

也可以显式检查或维护常驻代理：

```bash
xqshare-tunnel status
xqshare-tunnel restart
xqshare-tunnel stop
```

默认 `XQSHARE_TS_DAEMON=1`，客户端 sidecar 后台常驻。若需要回到旧行为，让 sidecar 随当前 Python 进程退出，可设置 `XQSHARE_TS_DAEMON=0`。

## Auth key 建议

- 服务端建议使用 reusable、non-ephemeral auth key。
- 客户端可以使用 reusable auth key，也可以首次用登录 URL 手动授权。
- 不要把 auth key 写入 `.env` 后提交仓库。
- 入网成功后，客户端默认把 tsnet 身份保存到用户级状态目录；重启通常不再需要 auth key。
- 可用 `XQSHARE_TS_DAEMON_DIR` 指定 pid/status/lock 目录，用 `XQSHARE_TS_STATE_DIR` 指定 tsnet 身份目录。
