[CmdletBinding()]
param(
    [ValidateSet("Start", "Stop", "Restart", "Status", "Test", "Demo", "Open")]
    [string]$Action = "Status",
    [string]$IsaacPython = "",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Output = "",
    [string]$RuntimeDirectory = "",
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($IsaacPython)) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        throw "Python executable not found; pass -IsaacPython explicitly."
    }
    $IsaacPython = $python.Source
}
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $ProjectRoot "outputs\web"
}
if ([string]::IsNullOrWhiteSpace($RuntimeDirectory)) {
    $RuntimeDirectory = Join-Path $ProjectRoot "outputs\local_stack"
}
$RuntimeDirectory = [System.IO.Path]::GetFullPath($RuntimeDirectory)
$Output = [System.IO.Path]::GetFullPath($Output)
$StatePath = Join-Path $RuntimeDirectory "service.json"
$Url = "http://${HostAddress}:$Port/"

function Assert-LocalConfiguration {
    if (-not (Test-Path -LiteralPath $IsaacPython -PathType Leaf)) {
        throw "Isaac Python not found: $IsaacPython"
    }
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "Port must be between 1 and 65535."
    }
    if ($Output -match '[^\x00-\x7F]') {
        throw "Use an ASCII-only output path when USD export is enabled on Windows."
    }
    if ($HostAddress -notin @("127.0.0.1", "localhost", "::1")) {
        throw "The no-auth local stack may only listen on localhost."
    }
}

function Get-ListenerInfo {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) {
        return $null
    }
    $listener = $listeners[0]
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Pid = [int]$listener.OwningProcess
        CommandLine = if ($null -ne $process) { [string]$process.CommandLine } else { "" }
    }
}

function Test-IsSceneFactoryProcess {
    param([Parameter(Mandatory = $true)]$Listener)
    return $Listener.CommandLine -match 'scene_factory\.webapp'
}

function Get-Health {
    return Invoke-RestMethod -Uri "${Url}api/health" -TimeoutSec 3
}

function Get-LlmStatus {
    return Invoke-RestMethod -Uri "${Url}api/llm/status" -TimeoutSec 3
}

function Show-LocalStatus {
    $listener = Get-ListenerInfo
    if ($null -eq $listener) {
        [pscustomobject]@{
            State = "stopped"
            Url = $Url
            Output = $Output
            Runtime = $IsaacPython
        } | Format-List
        return
    }
    if (-not (Test-IsSceneFactoryProcess $listener)) {
        [pscustomobject]@{
            State = "port-conflict"
            Url = $Url
            Pid = $listener.Pid
            Message = "Port $Port belongs to another process."
        } | Format-List
        return
    }

    try {
        $health = Get-Health
        $llm = Get-LlmStatus
        [pscustomobject]@{
            State = "running"
            Url = $Url
            Pid = $listener.Pid
            Output = $health.output_root
            Parser = $llm.parser
            LlmKeyConfigured = $llm.api_key_configured
            Transport = $llm.transport
            Proxy = $llm.proxy_url
        } | Format-List
    }
    catch {
        [pscustomobject]@{
            State = "starting-or-unhealthy"
            Url = $Url
            Pid = $listener.Pid
            Error = $_.Exception.Message
        } | Format-List
    }
}

function Stop-LocalService {
    $listener = Get-ListenerInfo
    if ($null -eq $listener) {
        Write-Host "SceneFactory is already stopped."
        if (Test-Path -LiteralPath $StatePath) {
            Remove-Item -LiteralPath $StatePath -Force
        }
        return
    }
    if (-not (Test-IsSceneFactoryProcess $listener)) {
        throw "Port $Port belongs to another process; refusing to stop PID $($listener.Pid)."
    }

    Stop-Process -Id $listener.Pid -Force
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Get-ListenerInfo) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-ListenerInfo) {
        throw "SceneFactory PID $($listener.Pid) did not release port $Port."
    }
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force
    }
    Write-Host "SceneFactory stopped."
}

function Start-LocalService {
    param([switch]$ForceRestart)
    Assert-LocalConfiguration

    $listener = Get-ListenerInfo
    if ($null -ne $listener) {
        if (-not (Test-IsSceneFactoryProcess $listener)) {
            throw "Port $Port belongs to another process; refusing to replace PID $($listener.Pid)."
        }
        if (-not $ForceRestart) {
            Write-Host "SceneFactory is already running at $Url"
            Show-LocalStatus
            return
        }
        Stop-LocalService
    }

    New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $Output -Force | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutPath = Join-Path $RuntimeDirectory "webapp-$stamp.stdout.log"
    $stderrPath = Join-Path $RuntimeDirectory "webapp-$stamp.stderr.log"
    $arguments = @(
        "-m", "scene_factory.webapp",
        "--host", $HostAddress,
        "--port", [string]$Port,
        "--output", $Output
    )

    $env:OMNI_KIT_ACCEPT_EULA = "YES"
    $process = Start-Process `
        -FilePath $IsaacPython `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            break
        }
        try {
            $health = Get-Health
            if ($health.ok) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 300
        }
    }

    if (-not $ready) {
        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
        $details = ""
        if (Test-Path -LiteralPath $stderrPath) {
            $details = (Get-Content -LiteralPath $stderrPath -Tail 30 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        }
        throw "SceneFactory did not become ready within 20 seconds.`n$details"
    }

    $serviceListener = Get-ListenerInfo
    if ($null -eq $serviceListener -or -not (Test-IsSceneFactoryProcess $serviceListener)) {
        throw "SceneFactory health check passed but its listener process could not be identified."
    }
    $state = [ordered]@{
        pid = $serviceListener.Pid
        launcher_pid = $process.Id
        started_at = (Get-Date).ToString("o")
        url = $Url
        python = $IsaacPython
        output = $Output
        stdout = $stdoutPath
        stderr = $stderrPath
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-Host "SceneFactory started without downloading anything."
    Show-LocalStatus
}

function Invoke-LocalTests {
    Assert-LocalConfiguration
    $previousMode = [Environment]::GetEnvironmentVariable("SCENE_FACTORY_LLM_MODE", "Process")
    try {
        $env:SCENE_FACTORY_LLM_MODE = "off"
        Push-Location $ProjectRoot
        try {
            & $IsaacPython -m compileall -q scene_factory tests
            if ($LASTEXITCODE -ne 0) {
                throw "Python compile check failed."
            }
            & $IsaacPython -m unittest discover -s tests -v
            if ($LASTEXITCODE -ne 0) {
                throw "Unit tests failed."
            }
            $node = Get-Command node -ErrorAction SilentlyContinue
            if ($null -ne $node) {
                & $node.Source --check web\app.js
                if ($LASTEXITCODE -ne 0) {
                    throw "JavaScript syntax check failed."
                }
            }
            else {
                Write-Host "Node.js is not installed; JavaScript syntax check skipped."
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        if ($null -eq $previousMode) {
            Remove-Item Env:\SCENE_FACTORY_LLM_MODE -ErrorAction SilentlyContinue
        }
        else {
            $env:SCENE_FACTORY_LLM_MODE = $previousMode
        }
    }
    Write-Host "Local no-download tests passed."
}

function New-OfflineDemo {
    Assert-LocalConfiguration
    $demoRoot = Join-Path (Split-Path -Parent $Output) "local_demo"
    $demoDir = Join-Path $demoRoot (Get-Date -Format "yyyyMMdd-HHmmss")
    New-Item -ItemType Directory -Path $demoDir -Force | Out-Null
    $previousMode = [Environment]::GetEnvironmentVariable("SCENE_FACTORY_LLM_MODE", "Process")
    try {
        $env:SCENE_FACTORY_LLM_MODE = "off"
        Push-Location $ProjectRoot
        try {
            & $IsaacPython -m scene_factory build `
                --recipe living_room_recent_snacking `
                --seed 42 `
                --output $demoDir `
                --usd
            if ($LASTEXITCODE -ne 0) {
                throw "Offline demo generation failed."
            }
            & $IsaacPython tools\validate_usd.py `
                (Join-Path $demoDir "scene.usd") `
                --report (Join-Path $demoDir "openusd_report.json")
            if ($LASTEXITCODE -ne 0) {
                throw "OpenUSD validation failed."
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        if ($null -eq $previousMode) {
            Remove-Item Env:\SCENE_FACTORY_LLM_MODE -ErrorAction SilentlyContinue
        }
        else {
            $env:SCENE_FACTORY_LLM_MODE = $previousMode
        }
    }
    Write-Host "Offline USD demo: $demoDir"
}

switch ($Action) {
    "Start" {
        Start-LocalService
        if ($OpenBrowser) {
            Start-Process $Url
        }
    }
    "Stop" {
        Stop-LocalService
    }
    "Restart" {
        Start-LocalService -ForceRestart
        if ($OpenBrowser) {
            Start-Process $Url
        }
    }
    "Status" {
        Show-LocalStatus
    }
    "Test" {
        Invoke-LocalTests
    }
    "Demo" {
        New-OfflineDemo
    }
    "Open" {
        $listener = Get-ListenerInfo
        if ($null -eq $listener -or -not (Test-IsSceneFactoryProcess $listener)) {
            throw "SceneFactory is not running. Start it first."
        }
        Start-Process $Url
    }
}
