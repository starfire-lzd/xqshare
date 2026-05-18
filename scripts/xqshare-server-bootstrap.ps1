param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 18812,
    [string]$AuthKey = $env:TS_AUTHKEY,
    [string]$StateDir = "$env:LOCALAPPDATA\xqshare\tsnet-server",
    [string]$PackagePath = "",
    [string]$QmtUserdataPath = $env:QMT_USERDATA_PATH,
    [int]$ReadyTimeoutSeconds = 300,
    [switch]$SkipPythonInstall,
    [switch]$SkipPackageInstall,
    [switch]$CheckOnly,
    [switch]$PauseOnExit
)

$ErrorActionPreference = "Stop"

$BootstrapLogDir = Join-Path $env:LOCALAPPDATA "xqshare\logs"
New-Item -ItemType Directory -Force -Path $BootstrapLogDir | Out-Null
$BootstrapLogPath = Join-Path $BootstrapLogDir "bootstrap.log"
$TranscriptStarted = $false
try {
    Start-Transcript -Path $BootstrapLogPath -Append | Out-Null
    $TranscriptStarted = $true
} catch {
}

trap {
    Write-Host ""
    Write-Host "xqshare server bootstrap failed." -ForegroundColor Red
    Write-Host "Error: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Log: $BootstrapLogPath"
    try {
        if ($TranscriptStarted) {
            Stop-Transcript | Out-Null
        }
    } catch {
    }
    if ($PauseOnExit) {
        Read-Host "Press Enter to exit"
    }
    exit 1
}

function Write-Step($Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Find-Python {
    $commands = @(
        @("py", "-3"),
        @("python"),
        @("python3")
    )

    foreach ($cmd in $commands) {
        try {
            $exe = $cmd[0]
            $args = @()
            if ($cmd.Length -gt 1) {
                $args = $cmd[1..($cmd.Length - 1)]
            }
            $path = & $exe @args -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path $path.Trim())) {
                return $path.Trim()
            }
        } catch {
        }
    }

    $searchRoots = @(
        "$env:LOCALAPPDATA\Microsoft\WindowsApps",
        "$env:LOCALAPPDATA\Programs\Python",
        "C:\Program Files",
        "C:\Program Files (x86)"
    )
    foreach ($root in $searchRoots) {
        if (-not (Test-Path $root)) {
            continue
        }
        try {
            $found = Get-ChildItem -Path $root -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch "\\Scripts\\|\\venv\\|\\.venv\\" } |
                Sort-Object FullName |
                Select-Object -First 1
            if ($found) {
                return $found.FullName
            }
        } catch {
        }
    }
    return $null
}

function Install-Python {
    if ($SkipPythonInstall) {
        throw "Python was not found and -SkipPythonInstall was set. Install Python 3.10+ first."
    }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python was not found and winget is unavailable. Install Python 3.10+ first."
    }

    Write-Step "Installing Python 3.12"
    $wingetArgs = @(
        "install",
        "--id", "Python.Python.3.12",
        "--exact",
        "--source", "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity"
    )
    $proc = Start-Process -FilePath "winget" -ArgumentList $wingetArgs -NoNewWindow -PassThru
    $spin = @("|", "/", "-", "\")
    $i = 0
    while (-not $proc.HasExited) {
        Write-Progress -Activity "Installing Python 3.12" -Status "winget is downloading/installing..." -PercentComplete (($i * 3) % 100)
        Write-Host -NoNewline ("`rInstalling Python 3.12 " + $spin[$i % $spin.Length])
        Start-Sleep -Seconds 1
        $i++
    }
    $proc.WaitForExit()
    $proc.Refresh()
    Write-Progress -Activity "Installing Python 3.12" -Completed
    Write-Host "`rInstalling Python 3.12 done.       "
    $python = Find-Python
    if ($python) {
        return $python
    }
    if ($null -ne $proc.ExitCode -and $proc.ExitCode -ne 0) {
        throw "winget failed to install Python. ExitCode=$($proc.ExitCode)"
    }

    $python = Find-Python
    if (-not $python) {
        $known = @(
            "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
            "C:\Program Files\Python312\python.exe"
        )
        foreach ($path in $known) {
            if (Test-Path $path) {
                return $path
            }
        }
        throw "Python was installed but python.exe was not found. Reopen PowerShell or check PATH."
    }
    return $python
}

function Resolve-PackageSpec {
    if ($PackagePath) {
        return (Resolve-Path $PackagePath).Path
    }

    $root = Resolve-Path (Join-Path $PSScriptRoot "..")
    $distDir = Join-Path $root "dist"
    $wheel = Get-ChildItem -Path $distDir -Filter "xqshare-*.whl" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($wheel) {
        return $wheel.FullName
    }

    if (Test-Path (Join-Path $root "pyproject.toml")) {
        return $root.Path
    }

    return "xqshare"
}

Write-Step "Checking Python"
$PythonExe = Find-Python
if (-not $PythonExe) {
    $PythonExe = Install-Python
}
Write-Host "Python: $PythonExe"
& $PythonExe --version

if (-not $SkipPackageInstall) {
    Write-Step "Installing pip tooling"
    Write-Host "This may take a few minutes on a new machine..."
    & $PythonExe -m pip install --upgrade pip setuptools wheel

    Write-Step "Installing xtquant"
    Write-Host "Downloading/installing xtquant and its dependencies..."
    & $PythonExe -m pip install --upgrade xtquant

    $packageSpec = Resolve-PackageSpec
    Write-Step "Installing xqshare: $packageSpec"
    Write-Host "Installing xqshare package..."
    & $PythonExe -m pip install --upgrade $packageSpec
}

Write-Step "Checking Tailscale state"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$resolvedStateDir = (Resolve-Path $StateDir).Path
if ($AuthKey) {
    $env:TS_AUTHKEY = $AuthKey
    Write-Host "AuthKey detected. It will be used for this startup."
} else {
    Remove-Item Env:TS_AUTHKEY -ErrorAction SilentlyContinue
    Write-Host "No AuthKey provided. Existing state will be reused; if this is first run, a Tailscale login URL will be shown."
}

if (-not $QmtUserdataPath) {
    $defaultQmt = "D:\develop\tools\DongguanQMT\userdata_mini"
    if (Test-Path $defaultQmt) {
        $QmtUserdataPath = $defaultQmt
    }
}
if ($QmtUserdataPath) {
    $env:QMT_USERDATA_PATH = $QmtUserdataPath
}

$env:XQSHARE_TAILSCALE = "1"
$env:XQSHARE_LOG_DIR = $BootstrapLogDir
$env:XQSHARE_TS_STATE_DIR = $resolvedStateDir
if (-not $env:XQSHARE_TS_HOSTNAME) {
    $env:XQSHARE_TS_HOSTNAME = "xqshare-server"
}
$env:XQSHARE_TS_TARGET_HOST = $HostAddress
$env:XQSHARE_TS_TARGET_PORT = "$Port"
$env:XQSHARE_TS_LISTEN_PORT = "$Port"
$env:XQSHARE_TS_READY_TIMEOUT = "$ReadyTimeoutSeconds"

Write-Step "Starting xqshare server"
Write-Host "Host: $HostAddress"
Write-Host "Port: $Port"
Write-Host "StateDir: $env:XQSHARE_TS_STATE_DIR"
Write-Host "ReadyTimeoutSeconds: $ReadyTimeoutSeconds"
Write-Host "QMT_USERDATA_PATH: $env:QMT_USERDATA_PATH"
Write-Host ""

if ($CheckOnly) {
    Write-Host "CheckOnly completed. Server was not started."
    try {
        if ($TranscriptStarted) {
            Stop-Transcript | Out-Null
        }
    } catch {
    }
    if ($PauseOnExit) {
        Read-Host "Press Enter to exit"
    }
    exit 0
}

& $PythonExe -m xqshare.server --tailscale --host $HostAddress --port $Port

Write-Host ""
Write-Host "xqshare server process exited." -ForegroundColor Yellow
Write-Host "Log: $BootstrapLogPath"
try {
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
} catch {
}
if ($PauseOnExit) {
    Read-Host "Press Enter to exit"
}
