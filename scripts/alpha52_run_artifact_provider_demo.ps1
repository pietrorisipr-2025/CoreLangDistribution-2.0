param(
  [string]$RepoRoot = ".",
  [string]$OutRoot = ".\examples\small_demo"
)

$ErrorActionPreference = "Stop"

Set-Location $RepoRoot

Write-Host "CLD2 alpha52.1 artifact provider demo"
Write-Host "RepoRoot: $RepoRoot"
Write-Host "OutRoot: $OutRoot"

New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null

python .\examples\small_demo\make_demo_data.py
python .\cld2.py pack .\examples\small_demo\release_v1 --out .\examples\small_demo\release_v1.cldrepo --release-id demo --release-seq 1 --force
python .\cld2.py pack .\examples\small_demo\release_v2 --out .\examples\small_demo\release_v2.cldrepo --release-id demo --release-seq 2 --force

$Result1 = Join-Path $OutRoot "alpha52_1_provider_result_learn_digest.json"

python .\scripts\alpha52_artifact_fetch_verify.py `
  --repo .\examples\small_demo\release_v2.cldrepo `
  --install .\examples\small_demo\install_v2 `
  --cache .\examples\small_demo\cache `
  --digest-mode tree-sha256 `
  --clean-install `
  --json-out $Result1

$Learned = Get-Content $Result1 -Raw | ConvertFrom-Json
if (-not $Learned.actual_sha256) {
  Write-Host "ERROR: learned digest is empty. Result:"
  Get-Content $Result1 -Raw
  exit 2
}

$Digest = $Learned.actual_sha256
Write-Host "Learned digest: $Digest"

$Result2 = Join-Path $OutRoot "alpha52_1_provider_result_verified.json"

python .\scripts\alpha52_artifact_fetch_verify.py `
  --repo .\examples\small_demo\release_v2.cldrepo `
  --install .\examples\small_demo\install_v2 `
  --cache .\examples\small_demo\cache `
  --digest-mode tree-sha256 `
  --expected-sha256 $Digest `
  --clean-install `
  --json-out $Result2

Write-Host "Demo verified result:"
Get-Content $Result2 -Raw
