param(
    [switch]$InstallIsaacProject,
    [switch]$NoMujoco
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Push-Location -LiteralPath $root
try {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        $uv = $uvCommand.Source
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if (-not $launcher) {
            throw "Python 3.12 launcher and uv are not available. Install Python 3.12 or uv first."
        }
        & py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)"
        if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required." }
        & py -3.12 -m uv --version 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "uv not found; installing uv into the current user's Python 3.12 tools..."
            & py -3.12 -m pip install --user uv
            if ($LASTEXITCODE -ne 0) { throw "uv installation failed." }
        }
        $uv = $null
    }
    $extra = if ($NoMujoco) { @() } else { @( "--extra", "mujoco" ) }
    if ($uv) {
        & $uv sync --locked --python 3.12 @extra
    } else {
        & py -3.12 -m uv sync --locked --python 3.12 @extra
    }
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed." }

    $corePython = Join-Path $root ".venv\Scripts\python.exe"
    $candidate = if ($env:SCENE_FACTORY_ISAAC_PYTHON) {
        $env:SCENE_FACTORY_ISAAC_PYTHON
    } else {
        Join-Path $root "local_resources\environments\scene_factory_isaac_py312\Scripts\python.exe"
    }
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $originalCandidate = [IO.Path]::GetFullPath($candidate)
        # Do not Resolve-Path the Isaac executable: that would turn the ASCII
        # junction back into the non-ASCII target path and reintroduce the USD bug.
        $env:SCENE_FACTORY_ISAAC_PYTHON_CANDIDATE = [IO.Path]::GetFullPath($candidate)
        $prepared = & $corePython -c "import os; from scene_factory.isaac_runtime import prepare_isaac_python; print(prepare_isaac_python(os.environ['SCENE_FACTORY_ISAAC_PYTHON_CANDIDATE']))" 2>$null
        $prepareExitCode = $LASTEXITCODE
        Remove-Item Env:SCENE_FACTORY_ISAAC_PYTHON_CANDIDATE -ErrorAction SilentlyContinue
        if ($prepareExitCode -eq 0 -and $prepared) {
            $candidate = ($prepared | Select-Object -Last 1).ToString().Trim()
        }
        & $candidate -c "import pxr" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $env:SCENE_FACTORY_ISAAC_PYTHON = $candidate
            Write-Host "Isaac Python: $env:SCENE_FACTORY_ISAAC_PYTHON"
            if ($candidate -ne $originalCandidate) {
                Write-Host "Isaac Python target: $originalCandidate"
            }
            if ($InstallIsaacProject) {
                & $candidate -m pip install --no-deps -e $root
                if ($LASTEXITCODE -ne 0) { throw "Installing project into Isaac Python failed." }
            }
        } else {
            Write-Warning "Isaac Python found but pxr import failed; Isaac integration remains optional."
        }
    } else {
        Write-Warning "No local Isaac Python found. Set SCENE_FACTORY_ISAAC_PYTHON when available."
    }
    & $corePython -m scene_factory.cli doctor
    if ($LASTEXITCODE -ne 0) { throw "Core environment diagnosis failed." }
    Write-Host "Ready: uv run scene-factory list-recipes (or py -3.12 -m uv run scene-factory list-recipes)"
} finally {
    Pop-Location
}
