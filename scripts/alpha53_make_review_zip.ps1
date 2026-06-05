param(
  [Parameter(Mandatory=$true)]
  [string]$Alpha53OutRoot,

  [Parameter(Mandatory=$true)]
  [string]$RepoRoot,

  [Parameter(Mandatory=$true)]
  [string]$ZipPath
)

$ErrorActionPreference = "Stop"

if (!(Test-Path $Alpha53OutRoot)) {
  throw "Alpha53OutRoot not found: $Alpha53OutRoot"
}
if (!(Test-Path $RepoRoot)) {
  throw "RepoRoot not found: $RepoRoot"
}

$ReviewDir = Join-Path (Split-Path -Parent $ZipPath) "alpha53_provider_trust_demo_REVIEW_UPLOAD"
if (Test-Path $ReviewDir) {
  Remove-Item $ReviewDir -Recurse -Force
}
New-Item -ItemType Directory -Path $ReviewDir -Force | Out-Null

Get-ChildItem $Alpha53OutRoot -Recurse -File -Include *.json,*.md,*.txt,*.log,*.csv | ForEach-Object {
  $Rel = $_.FullName.Substring($Alpha53OutRoot.Length).TrimStart('\')
  $Dest = Join-Path $ReviewDir $Rel
  New-Item -ItemType Directory -Path (Split-Path $Dest -Parent) -Force | Out-Null
  Copy-Item $_.FullName $Dest -Force
}

$ExtraFiles = @(
  (Join-Path $RepoRoot "scripts\alpha53_validate_provider_result.py"),
  (Join-Path $RepoRoot "scripts\alpha53_run_provider_trust_demo.ps1"),
  (Join-Path $RepoRoot "scripts\alpha53_make_review_zip.ps1"),
  (Join-Path $RepoRoot "docs\ALPHA53_PROVIDER_TRUST_MODEL.md"),
  (Join-Path $RepoRoot "docs\ALPHA53_PROVIDER_POLICY_SPEC.md"),
  (Join-Path $RepoRoot "docs\ALPHA53_CLAIM_BOUNDARY.md"),
  (Join-Path $RepoRoot "examples\alpha53_provider_policy.example.json"),
  (Join-Path $RepoRoot "tests\README_ALPHA53_TESTING.md"),
  (Join-Path $RepoRoot "ALPHA53_TRUST_MODEL_PROVIDER_POLICY_PLAN.md")
)

foreach ($F in $ExtraFiles) {
  if (Test-Path $F) {
    Copy-Item $F (Join-Path $ReviewDir (Split-Path $F -Leaf)) -Force
  }
}

@"
# CLD2 alpha53 provider trust demo review

Included:
- positive validation JSON
- negative control JSON files
- trust demo summary/report
- alpha53 scripts and docs

Expected:
- positive_validation_ok = true
- tampered_digest_rejected = true
- audit_failure_rejected = true
- overall_ok = true
"@ | Set-Content -Encoding UTF8 (Join-Path $ReviewDir "README_REVIEW_UPLOAD.md")

Get-ChildItem $ReviewDir -Recurse -File | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ReviewDir.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthKB = [Math]::Round($_.Length / 1KB, 2)
  }
} | Sort-Object RelativePath | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "REVIEW_FILE_INVENTORY.csv")

if (Test-Path $ZipPath) {
  Remove-Item $ZipPath -Force
}
Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $ZipPath -Force

Write-Host "Review ZIP:"
Write-Host $ZipPath
Get-Item $ZipPath | Select-Object FullName, Length, LastWriteTime
