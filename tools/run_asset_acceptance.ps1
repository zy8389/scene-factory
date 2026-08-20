param(
    [Parameter(Mandatory = $true)]
    [string]$AssetUsd,
    [string]$IsaacPython = "F:\scene_factory_isaac_py312\Scripts\python.exe",
    [string]$Output = "F:\scene_factory_runtime\asset_acceptance",
    [double]$MassKg = 1.0,
    [double]$DropHeightM = 1.0,
    [int]$Steps = 180,
    [string]$Record,
    [string]$CollisionUsd
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AssetUsd = [System.IO.Path]::GetFullPath($AssetUsd)
$Output = [System.IO.Path]::GetFullPath($Output)

if (-not (Test-Path -LiteralPath $IsaacPython -PathType Leaf)) {
    throw "Isaac Python not found: $IsaacPython"
}
if (-not (Test-Path -LiteralPath $AssetUsd -PathType Leaf)) {
    throw "Asset USD not found: $AssetUsd"
}
if ($AssetUsd -match '[^\x00-\x7F]' -or $Output -match '[^\x00-\x7F]') {
    throw "On Windows, use ASCII-only USD and output paths for Isaac Sim"
}
if ($MassKg -le 0 -or $DropHeightM -lt 0 -or $Steps -lt 1) {
    throw "MassKg and Steps must be positive; DropHeightM must be non-negative"
}

New-Item -ItemType Directory -Path $Output -Force | Out-Null
$env:OMNI_KIT_ACCEPT_EULA = "YES"
$InspectionReport = Join-Path $Output "asset_inspection.json"
$DropScene = Join-Path $Output "drop_test.usda"
$DropSceneReport = Join-Path $Output "drop_scene_report.json"
$RuntimeReport = Join-Path $Output "physx_report.json"

Push-Location $ProjectRoot
try {
    & $IsaacPython "$PSScriptRoot\prepare_asset.py" inspect `
        $AssetUsd `
        --report $InspectionReport
    if ($LASTEXITCODE -ne 0) { throw "Asset inspection failed: $LASTEXITCODE" }

    $DropSceneArgs = @(
        "$PSScriptRoot\prepare_asset.py", "drop-scene", $AssetUsd,
        "--output", $DropScene, "--report", $DropSceneReport,
        "--mass-kg", $MassKg, "--height", $DropHeightM
    )
    if ($CollisionUsd) {
        $DropSceneArgs += @("--collision", [System.IO.Path]::GetFullPath($CollisionUsd))
    }
    & $IsaacPython @DropSceneArgs
    if ($LASTEXITCODE -ne 0) { throw "Drop-test scene generation failed: $LASTEXITCODE" }

    $RuntimeArgs = @(
        "$PSScriptRoot\validate_isaac_runtime.py", $DropScene,
        "--steps", $Steps, "--report", $RuntimeReport
    )
    if ($CollisionUsd) {
        $RuntimeArgs += "--collision-required"
    }
    & $IsaacPython @RuntimeArgs
    if ($LASTEXITCODE -ne 0) { throw "PhysX runtime validation failed: $LASTEXITCODE" }

    if ($Record) {
        $Record = [System.IO.Path]::GetFullPath($Record)
        & $IsaacPython "$PSScriptRoot\prepare_asset.py" promote `
            $Record `
            $RuntimeReport
        if ($LASTEXITCODE -ne 0) { throw "Asset promotion failed: $LASTEXITCODE" }
    }

    Write-Host "Asset acceptance passed: $Output"
    Write-Host "PhysX report: $RuntimeReport"
}
finally {
    Pop-Location
}
