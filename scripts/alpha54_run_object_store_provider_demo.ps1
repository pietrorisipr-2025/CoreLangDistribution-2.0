param(
  [Parameter(Mandatory=$true)][string]$RepoRoot,
  [Parameter(Mandatory=$true)][string]$OutRoot,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Write-Host "=== CLD2 alpha54 object-store provider demo ==="
Write-Host "RepoRoot: $RepoRoot"
Write-Host "OutRoot:  $OutRoot"

if (!(Test-Path $RepoRoot)) { throw "RepoRoot not found: $RepoRoot" }
if (!(Test-Path (Join-Path $RepoRoot "cld2.py"))) { throw "cld2.py not found in RepoRoot" }
if (!(Test-Path (Join-Path $RepoRoot "scripts\alpha54_object_store_provider_demo.py"))) { throw "alpha54_object_store_provider_demo.py not found" }

if (Test-Path $OutRoot) { Remove-Item $OutRoot -Recurse -Force }
New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null

Set-Location $RepoRoot

& $Python ".\scripts\alpha54_object_store_provider_demo.py" --repo-root $RepoRoot --out-root $OutRoot --python $Python
$rc = $LASTEXITCODE

Write-Host ""
Write-Host "Exit code: $rc"
Write-Host "Summary exists:" (Test-Path (Join-Path $OutRoot "alpha54_object_store_provider_summary.json"))
Write-Host "Verified exists:" (Test-Path (Join-Path $OutRoot "alpha54_provider_result_verified.json"))

if (Test-Path (Join-Path $OutRoot "alpha54_object_store_provider_summary.json")) {
  Get-Content (Join-Path $OutRoot "alpha54_object_store_provider_summary.json") -Raw
}

exit $rc
