param(
    [string]$Python = "",
    [string]$IsaacPython = "",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [string]$Output = "",
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Test-PxrRuntime {
    param([string]$PythonPath)

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return $false
    }
    & $PythonPath -c "import pxr" 2>$null
    return $LASTEXITCODE -eq 0
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $projectPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
        $Python = $projectPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw "Python executable not found; pass -Python explicitly."
        }
        $Python = $pythonCommand.Source
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python"
}
& $Python -c "import mujoco" 2>$null
$MujocoAvailable = $LASTEXITCODE -eq 0

if (-not [string]::IsNullOrWhiteSpace($IsaacPython)) {
    if (-not (Test-PxrRuntime $IsaacPython)) {
        throw "Isaac Python cannot import pxr: $IsaacPython"
    }
} else {
    $candidates = @()
    $configuredIsaacPython = [Environment]::GetEnvironmentVariable(
        "SCENE_FACTORY_ISAAC_PYTHON",
        "Process"
    )
    if (-not [string]::IsNullOrWhiteSpace($configuredIsaacPython)) {
        $candidates += $configuredIsaacPython
    }
    $workspaceParent = Split-Path $ProjectRoot -Parent
    $candidates += Join-Path $workspaceParent "scene_factory_isaac_py312\Scripts\python.exe"
    $fallbackPython = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $fallbackPython) {
        $candidates += $fallbackPython.Source
    }

    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-PxrRuntime $candidate) {
            $IsaacPython = $candidate
            break
        }
    }
}
if (-not [string]::IsNullOrWhiteSpace($IsaacPython)) {
    $env:SCENE_FACTORY_ISAAC_PYTHON = $IsaacPython
}
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $ProjectRoot "outputs\web"
    if ($Output -match '[^\x00-\x7F]') {
        $Output = Join-Path (Split-Path $ProjectRoot -Parent) "scene_factory_runtime\web"
    }
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
    if ($MujocoAvailable) {
        Write-Host "MuJoCo environment: available ($Python)"
    } else {
        Write-Host "MuJoCo environment: unavailable (optional; install with: python -m pip install .[mujoco])"
    }
    if ([string]::IsNullOrWhiteSpace($IsaacPython)) {
        Write-Host "Isaac validation: unavailable (optional)"
    } else {
        Write-Host "Isaac validation: $IsaacPython"
    }
    Write-Host "SceneFactory UI: http://${HostAddress}:$Port/"
    & $Python -m scene_factory.webapp `
        --host $HostAddress `
        --port $Port `
        --output $Output
}
finally {
    Pop-Location
}
