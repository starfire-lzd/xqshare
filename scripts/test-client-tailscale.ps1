param(
    [string]$ServerHost = "xqshare-server",
    [int]$Port = 18812,
    [string]$AuthKey = $env:TS_AUTHKEY,
    [string]$ClientId = $env:XQSHARE_CLIENT_ID,
    [string]$ClientSecret = $env:XQSHARE_CLIENT_SECRET
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

python -m pip install -e .
# The Tailscale sidecar is prebuilt and packaged under xqshare/bin.

if ($AuthKey) {
    $env:TS_AUTHKEY = $AuthKey
}
if ($ClientId) {
    $env:XQSHARE_CLIENT_ID = $ClientId
}
if ($ClientSecret) {
    $env:XQSHARE_CLIENT_SECRET = $ClientSecret
}

$env:XQSHARE_TAILSCALE = "1"
$env:XQSHARE_REMOTE_HOST = $ServerHost
$env:XQSHARE_REMOTE_PORT = "$Port"
$env:XQSHARE_TS_TARGET_HOST = $ServerHost
$env:XQSHARE_TS_TARGET_PORT = "$Port"
$env:XQSHARE_TS_LOCAL_HOST = "127.0.0.1"
$env:XQSHARE_TS_LOCAL_PORT = "$Port"

python -c "from xqshare import XtQuantRemote; xt=XtQuantRemote(); print(xt.get_service_status()); xt.close()"
