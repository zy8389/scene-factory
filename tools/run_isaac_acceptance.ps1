param(
    [string]$IsaacPython = "",
    [string]$Output = "",
    [string]$Recipe = "kitchen_after_cooking",
    [int]$Seed = 77,
    [int]$Steps = 240
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
    $Output = Join-Path $ProjectRoot "outputs\acceptance"
}
$Output = [System.IO.Path]::GetFullPath($Output)

if (-not (Test-Path -LiteralPath $IsaacPython -PathType Leaf)) {
    throw "Isaac Python not found: $IsaacPython"
}
if ($Output -match '[^\x00-\x7F]') {
    throw "On Windows, use an ASCII-only Isaac output path: $Output"
}

New-Item -ItemType Directory -Path $Output -Force | Out-Null
$env:OMNI_KIT_ACCEPT_EULA = "YES"

Push-Location $ProjectRoot
try {
    & $IsaacPython -m scene_factory build `
        --recipe $Recipe `
        --seed $Seed `
        --output $Output `
        --usd
    if ($LASTEXITCODE -ne 0) { throw "SceneFactory USD export failed: $LASTEXITCODE" }

    & $IsaacPython "$PSScriptRoot\validate_usd.py" `
        "$Output\scene.usd" `
        --report "$Output\openusd_report.json"
    if ($LASTEXITCODE -ne 0) { throw "OpenUSD validation failed: $LASTEXITCODE" }

    & $IsaacPython "$PSScriptRoot\validate_isaac_runtime.py" `
        "$Output\scene.usd" `
        --steps $Steps `
        --report "$Output\isaac_runtime_report.json"
    if ($LASTEXITCODE -ne 0) { throw "Isaac runtime validation failed: $LASTEXITCODE" }

    Write-Host "Isaac acceptance passed: $Output"
}
finally {
    Pop-Location
}
