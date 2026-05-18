# Tailscale 自动内网穿透

目标：服务端和客户端都只运行命令行脚本，由 Python 自动拉起对应系统的 Tailscale sidecar。

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
.\scripts\test-client-tailscale.ps1 -ServerHost xqshare-server
```

脚本会启动客户端 sidecar，本地监听 `127.0.0.1:18812`，再通过 tailnet 连接 `xqshare-server:18812`。

## 客户端 macOS/Linux

```bash
cd /path/to/xqshare
export TS_AUTHKEY="tskey-auth-xxxxx"   # 首次接入需要；已有 tsnet-state-client 后可省略
export XQSHARE_CLIENT_ID="enterprise-user"
export XQSHARE_CLIENT_SECRET="your-secret"
./scripts/test-client-tailscale.sh xqshare-server 18812
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

## 命令行工具自动模式

```bash
export XQSHARE_TAILSCALE=1
xtdata --tailscale get_stock_list_in_sector --sector-name "沪深A股"
```

## Auth key 建议

- 服务端建议使用 reusable、non-ephemeral auth key。
- 客户端可以使用 reusable auth key，也可以首次用登录 URL 手动授权。
- 不要把 auth key 写入 `.env` 后提交仓库。
- 入网成功后，`tailscale-proxy/tsnet-state*` 会保存节点身份；重启通常不再需要 auth key。
