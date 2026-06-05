param(
  [Parameter(Mandatory=$true)][string]$RepoRoot,
  [Parameter(Mandatory=$true)][string]$OutRoot,
  [int]$SizeMiB = 64
)

$ErrorActionPreference = "Stop"

Write-Host "=== CLD2 alpha55 public examples ==="
Write-Host "RepoRoot: $RepoRoot"
Write-Host "OutRoot:  $OutRoot"
Write-Host "SizeMiB:   $SizeMiB"
Write-Host ""

if (!(Test-Path $RepoRoot)) { throw "RepoRoot not found: $RepoRoot" }
Set-Location $RepoRoot

if (!(Test-Path ".\cld2.py")) { throw "cld2.py not found in RepoRoot" }
if (!(Test-Path ".\scripts\alpha55_make_public_examples.py")) { throw "alpha55_make_public_examples.py not found" }
if (!(Test-Path ".\scripts\alpha55_run_public_examples_matrix.py")) { throw "alpha55_run_public_examples_matrix.py not found" }

$ExamplesRoot = Join-Path $OutRoot "generated_examples"
$MatrixOut = Join-Path $OutRoot "matrix"
$ReviewDir = Join-Path $OutRoot "alpha55_public_examples_REVIEW_UPLOAD"
$Zip = Join-Path $OutRoot "alpha55_public_examples_REVIEW_UPLOAD.zip"

if (Test-Path $OutRoot) { Remove-Item $OutRoot -Recurse -Force }
New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null

Write-Host "Generating examples..."
python .\scripts\alpha55_make_public_examples.py --out-dir $ExamplesRoot --size-mib $SizeMiB --force
if ($LASTEXITCODE -ne 0) { throw "Example generation failed" }

Write-Host ""
Write-Host "Running CLD2 matrix..."
python .\scripts\alpha55_run_public_examples_matrix.py --repo-root $RepoRoot --examples-root $ExamplesRoot --out-root $MatrixOut
$MatrixRc = $LASTEXITCODE

Write-Host ""
Write-Host "Matrix exit code: $MatrixRc"

Write-Host "Creating lightweight review ZIP..."
if (Test-Path $ReviewDir) { Remove-Item $ReviewDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReviewDir -Force | Out-Null

$AllowedExt = @(".json", ".csv", ".md", ".txt", ".log", ".py", ".ps1", ".sha256")
$MaxSingleFileBytes = 25MB

Get-ChildItem $OutRoot -Recurse -File | Where-Object {
  $AllowedExt -contains $_.Extension.ToLower() -and
  $_.Length -le $MaxSingleFileBytes -and
  $_.FullName -notlike "*$ReviewDir*"
} | ForEach-Object {
  $Rel = $_.FullName.Substring($OutRoot.Length).TrimStart('\')
  $Dest = Join-Path $ReviewDir $Rel
  $Parent = Split-Path $Dest -Parent
  New-Item -ItemType Directory -Path $Parent -Force | Out-Null
  Copy-Item $_.FullName $Dest -Force
}

$ExtraFiles = @(
  (Join-Path $RepoRoot "docs\ALPHA55_PUBLIC_EXAMPLES.md"),
  (Join-Path $RepoRoot "docs\ALPHA55_CLAIM_BOUNDARY.md"),
  (Join-Path $RepoRoot "docs\ALPHA55_GITHUB_SHOWCASE_NOTES.md"),
  (Join-Path $RepoRoot "tests\README_ALPHA55_TESTING.md"),
  (Join-Path $RepoRoot "README_ALPHA55_PUBLIC_EXAMPLES_PACK.md")
)
foreach ($F in $ExtraFiles) {
  if (Test-Path $F) {
    Copy-Item $F (Join-Path $ReviewDir (Split-Path $F -Leaf)) -Force
  }
}

$ReadmePath = Join-Path $ReviewDir "README_REVIEW_UPLOAD.md"
$Readme = @"
# CLD2 alpha55 public examples review upload

Included:
- generated examples summary metadata
- per-scenario CLD2 result JSON
- aggregate CSV/Markdown report
- alpha55 docs and scripts

Expected:
- overall_ok = true
- per-scenario audit_v2_ok = true
- per-scenario digest_ok = true

Claim boundary:
Local generated public examples only. Not a cloud/CDN/S3/MinIO benchmark.

Created:
$(Get-Date -Format o)
"@
Set-Content -Path $ReadmePath -Value $Readme -Encoding UTF8

Get-ChildItem $ReviewDir -Recurse -File | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ReviewDir.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthKB = [Math]::Round($_.Length / 1KB, 2)
  }
} | Sort-Object RelativePath | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "REVIEW_FILE_INVENTORY.csv")

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $Zip -Force

Write-Host ""
Write-Host "Review ZIP:"
Write-Host $Zip
Write-Host "Exists =" (Test-Path $Zip)
if (Test-Path $Zip) {
  Get-Item $Zip | Select-Object FullName, Length, LastWriteTime
  Write-Host "Size MB =" ([Math]::Round((Get-Item $Zip).Length / 1MB, 3))
}

exit $MatrixRc
