param(
  [Parameter(Mandatory=$true)][string]$Alpha54OutRoot,
  [Parameter(Mandatory=$true)][string]$RepoRoot,
  [Parameter(Mandatory=$true)][string]$ZipPath
)

$ErrorActionPreference = "Stop"

$ReviewDir = [System.IO.Path]::Combine([System.IO.Path]::GetDirectoryName($ZipPath), "alpha54_object_store_provider_demo_REVIEW_UPLOAD")

if (!(Test-Path $Alpha54OutRoot)) { throw "Alpha54OutRoot not found: $Alpha54OutRoot" }
if (!(Test-Path $RepoRoot)) { throw "RepoRoot not found: $RepoRoot" }

if (Test-Path $ReviewDir) { Remove-Item $ReviewDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReviewDir -Force | Out-Null

Get-ChildItem $Alpha54OutRoot -Recurse -File -Include *.json,*.md,*.txt,*.log,*.csv,*.py | ForEach-Object {
  $rel = $_.FullName.Substring($Alpha54OutRoot.Length).TrimStart('\')
  $dest = Join-Path $ReviewDir $rel
  $parent = Split-Path $dest -Parent
  New-Item -ItemType Directory -Path $parent -Force | Out-Null
  Copy-Item $_.FullName $dest -Force
}

$ExtraFiles = @(
  (Join-Path $RepoRoot "scripts\alpha54_object_store_provider_demo.py"),
  (Join-Path $RepoRoot "scripts\alpha54_run_object_store_provider_demo.ps1"),
  (Join-Path $RepoRoot "docs\ALPHA54_OBJECT_STORE_PROVIDER_GUIDE.md"),
  (Join-Path $RepoRoot "docs\ALPHA54_OBJECT_STORE_REQUEST_SPEC.md"),
  (Join-Path $RepoRoot "docs\ALPHA54_MINIO_OPTIONAL_NOTES.md"),
  (Join-Path $RepoRoot "docs\ALPHA54_CLAIM_BOUNDARY.md"),
  (Join-Path $RepoRoot "examples\alpha54_object_store_request.example.json"),
  (Join-Path $RepoRoot "tests\README_ALPHA54_TESTING.md")
)

foreach ($f in $ExtraFiles) {
  if (Test-Path $f) { Copy-Item $f (Join-Path $ReviewDir (Split-Path $f -Leaf)) -Force }
}

$readme = @(
  "# CLD2 alpha54 object-store provider demo review",
  "",
  "Included:",
  "- local object-store-like provider demo results",
  "- request JSON",
  "- provider wrapper/demo scripts",
  "- alpha54 docs",
  "",
  "Expected:",
  "- overall_ok = true",
  "- learned_digest_ok = true",
  "- verified_digest_ok = true",
  "- verified_audit_ok = true",
  "",
  "Created:",
  (Get-Date -Format o)
)
$readme | Set-Content -Encoding UTF8 (Join-Path $ReviewDir "README_REVIEW_UPLOAD.md")

Get-ChildItem $ReviewDir -Recurse -File | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ReviewDir.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthKB = [Math]::Round($_.Length / 1KB, 2)
  }
} | Sort-Object RelativePath | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "REVIEW_FILE_INVENTORY.csv")

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $ZipPath -Force

Write-Host "ZIP review created: $ZipPath"
Get-Item $ZipPath | Select-Object FullName, Length, LastWriteTime
