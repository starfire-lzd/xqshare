param(
    [string]$AuthKey = $env:TS_AUTHKEY,
    [string]$Hostname = "xqshare-server",
    [int]$Port = 18812,
    [string]$QmtUserdataPath = $env:QMT_USERDATA_PATH
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

python -m pip install -e .
# The Tailscale sidecar is prebuilt and packaged under xqshare/bin.

if ($AuthKey) {
    $env:TS_AUTHKEY = $AuthKey
}
if ($QmtUserdataPath) {
    $env:QMT_USERDATA_PATH = $QmtUserdataPath
}

$env:XQSHARE_TAILSCALE = "1"
$env:XQSHARE_TS_HOSTNAME = $Hostname
$env:XQSHARE_TS_TARGET_HOST = "127.0.0.1"
$env:XQSHARE_TS_TARGET_PORT = "$Port"
$env:XQSHARE_TS_LISTEN_PORT = "$Port"

python -m xqshare.server --tailscale --host 127.0.0.1 --port $Port
