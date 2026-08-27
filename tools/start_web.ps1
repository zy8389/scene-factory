param(
    [string]$IsaacPython = "",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Output = "",
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
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

if (-not (Test-Path -LiteralPath $IsaacPython -PathType Leaf)) {
    throw "Isaac Python not found: $IsaacPython"
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535"
}
if ($Output -match '[^\x00-\x7F]') {
    throw "Use an ASCII-only output path when USD export is enabled on Windows"
}

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    if (-not $Restart) {
        throw "Port $Port is already in use. Pass -Restart to replace an existing SceneFactory web service."
    }
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if ($process.CommandLine -notmatch 'scene_factory\.webapp') {
            throw "Port $Port belongs to another process; refusing to stop PID $($listener.OwningProcess)"
        }
        Stop-Process -Id $listener.OwningProcess -Force
    }
}

$env:OMNI_KIT_ACCEPT_EULA = "YES"
Push-Location $ProjectRoot
try {
    Write-Host "LLM config: $ProjectRoot\config\llm.json"
    Write-Host "LLM key env: SCENE_FACTORY_LLM_API_KEY"
    Write-Host "SceneFactory UI: http://${HostAddress}:$Port/"
    & $IsaacPython -m scene_factory.webapp `
        --host $HostAddress `
        --port $Port `
        --output $Output
}
finally {
    Pop-Location
}
