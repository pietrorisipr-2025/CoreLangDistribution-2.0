param(
  [string]$RepoRoot = (Get-Location).Path,
  [string]$ResultRoot = "",
  [string]$CodeVersion = "",
  [string]$BenchmarkMilestone = "alpha51.2"
)

Write-Host "=== CLD2 alpha51.2 metadata cleanup ==="
Write-Host "RepoRoot: $RepoRoot"

if (!(Test-Path $RepoRoot)) {
  Write-Host "ERRORE: repo non trovata: $RepoRoot"
  exit 1
}

Set-Location $RepoRoot

if (-not $CodeVersion) {
  $InitFile = Join-Path $RepoRoot "corelangdistribution2\__init__.py"
  if (Test-Path $InitFile) {
    $InitText = Get-Content $InitFile -Raw
    if ($InitText -match '__version__\s*=\s*"([^"]+)"') {
      $CodeVersion = $Matches[1]
    }
  }
}

if (-not $CodeVersion) { $CodeVersion = "2.0.0-alpha50.2" }

Write-Host "CodeVersion: $CodeVersion"
Write-Host "BenchmarkMilestone: $BenchmarkMilestone"

$PatchLogDir = Join-Path $RepoRoot "17_alpha51_2_metadata_cleanup_log"
if (Test-Path $PatchLogDir) { Remove-Item $PatchLogDir -Recurse -Force }
New-Item -ItemType Directory -Path $PatchLogDir -Force | Out-Null

$Targets = @()
$Targets += Get-ChildItem (Join-Path $RepoRoot "scripts") -File -Include "alpha51*.py", "alpha51*.ps1" -Recurse -ErrorAction SilentlyContinue
$Targets += Get-ChildItem (Join-Path $RepoRoot "docs") -File -Include "ALPHA51*.md", "WHEN_TO_USE_CLD2.md", "ARTIFACT_PROVIDER_MODE.md" -Recurse -ErrorAction SilentlyContinue

$Changed = @()
foreach ($File in $Targets) {
  $Text = Get-Content $File.FullName -Raw -ErrorAction SilentlyContinue
  if ($null -eq $Text) { continue }
  $New = $Text.Replace(("2.0-" + "alpha32"), $CodeVersion).Replace(("2.0.0-" + "alpha32"), $CodeVersion)
  if ($New -ne $Text) {
    Set-Content -Path $File.FullName -Value $New -Encoding UTF8
    $Changed += [PSCustomObject]@{ Path = $File.FullName; Change = "legacy version replaced" }
  }
}

$Report = Join-Path $PatchLogDir "ALPHA51_2_REPO_METADATA_PATCH_REPORT.md"
$ChangedRows = if ($Changed.Count -gt 0) {
  ($Changed | ForEach-Object { "| `$($_.Path)` | $($_.Change) |" }) -join "`n"
} else {
  "| _none_ | no legacy version strings found |"
}

@"
# CLD2 alpha51.2 repo metadata patch report

- Code baseline version: `$CodeVersion`
- Benchmark harness milestone: `$BenchmarkMilestone`
- Core logic changed: no

## Changed repo files

| File | Change |
|---|---|
$ChangedRows
"@ | Set-Content -Encoding UTF8 $Report

$Changed | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $PatchLogDir "changed_repo_files.json")

if ($ResultRoot -and (Test-Path $ResultRoot)) {
  Write-Host "ResultRoot presente: correggo anche i risultati esistenti."
  python .\scripts\alpha51_2_fix_result_metadata.py --result-root $ResultRoot --code-version $CodeVersion --benchmark-milestone $BenchmarkMilestone
  powershell -ExecutionPolicy Bypass -File .\scripts\alpha51_2_make_review_zip.ps1 -ResultRoot $ResultRoot -ZipName "alpha51_2_synthetic_matrix_REVIEW_UPLOAD.zip"
} elseif ($ResultRoot) {
  Write-Host "ATTENZIONE: ResultRoot indicato ma non trovato: $ResultRoot"
}

Write-Host "Patch report:"
Write-Host $Report
Write-Host "Done."
