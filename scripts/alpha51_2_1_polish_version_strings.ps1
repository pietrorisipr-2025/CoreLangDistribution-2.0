param(
  [Parameter(Mandatory=$true)] [string] $RepoRoot,
  [Parameter(Mandatory=$true)] [string] $ResultRoot,
  [string] $CodeVersion = "2.0.0-alpha50.2",
  [string] $BenchmarkMilestone = "alpha51.2.1"
)

$ErrorActionPreference = "Stop"

Write-Host "=== CLD2 alpha51.2.1 version string polish ==="
Write-Host "RepoRoot:   $RepoRoot"
Write-Host "ResultRoot: $ResultRoot"
Write-Host "CodeVersion: $CodeVersion"
Write-Host "BenchmarkMilestone: $BenchmarkMilestone"
Write-Host ""

if (!(Test-Path $RepoRoot)) {
  throw "RepoRoot not found: $RepoRoot"
}
if (!(Test-Path $ResultRoot)) {
  throw "ResultRoot not found: $ResultRoot"
}

$AllowedExt = @(".json", ".csv", ".md", ".html", ".txt", ".log", ".ps1", ".py")
$BadStrings = @(
  ("2.0-" + "2.0.0-alpha50.2"),
  ("2.0-" + "alpha32"),
  ("2.0.0-" + "alpha32")
)

$Touched = New-Object System.Collections.Generic.List[object]

function Patch-TextFile {
  param([string] $Path)

  $ext = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
  if ($AllowedExt -notcontains $ext) { return }

  $old = Get-Content -Path $Path -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
  if ($null -eq $old) { return }

  $new = $old
  foreach ($b in $BadStrings) {
    $new = $new.Replace($b, $CodeVersion)
  }

  # Normalize known alpha51 milestone metadata where present, without touching CLD2 code version.
  $new = $new.Replace('"benchmark_milestone": "alpha51.2"', '"benchmark_milestone": "alpha51.2.1"')
  $new = $new.Replace('benchmark milestone = alpha51.2', 'benchmark milestone = alpha51.2.1')
  $new = $new.Replace('benchmark milestone: alpha51.2', 'benchmark milestone: alpha51.2.1')
  $new = $new.Replace('Alpha51.2', 'Alpha51.2.1')
  $new = $new.Replace('alpha51.2 metadata cleanup', 'alpha51.2.1 version string polish')

  if ($new -ne $old) {
    Set-Content -Path $Path -Value $new -Encoding UTF8
    $Touched.Add([PSCustomObject]@{ Path = $Path; Bytes = (Get-Item $Path).Length }) | Out-Null
  }
}

Write-Host "Patching result files..."
Get-ChildItem $ResultRoot -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
  Patch-TextFile $_.FullName
}

Write-Host "Patching alpha51 scripts/docs in repo..."
$RepoTargets = @(
  (Join-Path $RepoRoot "scripts"),
  (Join-Path $RepoRoot "docs")
) | Where-Object { Test-Path $_ }

foreach ($rt in $RepoTargets) {
  Get-ChildItem $rt -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like "alpha51*" -or $_.Name -like "ALPHA51*" -or $_.Name -eq "WHEN_TO_USE_CLD2.md" -or $_.Name -eq "ARTIFACT_PROVIDER_MODE.md"
  } | ForEach-Object {
    Patch-TextFile $_.FullName
  }
}

# Write a final report.
$ReportPath = Join-Path $ResultRoot "ALPHA51_2_1_VERSION_STRING_POLISH_REPORT.md"
$JsonPath = Join-Path $ResultRoot "alpha51_2_1_version_string_polish.json"

$Now = Get-Date -Format o
$Report = @"
# CLD2 alpha51.2.1 — version string polish report

Created: `$Now`

## Scope

- Code baseline: `$CodeVersion`
- Benchmark milestone: `$BenchmarkMilestone`
- ResultRoot: `$ResultRoot`
- RepoRoot: `$RepoRoot`

## Fix applied

Replaced malformed/legacy version strings:

```text
malformed legacy alpha50.2-prefixed version string
legacy alpha32 marker
legacy alpha32 dotted marker
```

with:

```text
$CodeVersion
```

No benchmark was rerun. No CLD2 core logic was changed.

## Files touched

"@

if ($Touched.Count -eq 0) {
  $Report += "\nNo files needed changes.\n"
} else {
  foreach ($t in $Touched) {
    $rel = $t.Path
    $Report += "\n- `$rel`"
  }
  $Report += "\n"
}

Set-Content -Path $ReportPath -Value $Report -Encoding UTF8

# Scan for remaining problematic references.
$ScanFiles = Get-ChildItem $ResultRoot -Recurse -File -Include *.json,*.csv,*.md,*.html,*.txt,*.log -ErrorAction SilentlyContinue
$RemainingBad = @()
foreach ($sf in $ScanFiles) {
  $txt = Get-Content -Path $sf.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
  if ($null -eq $txt) { continue }
  foreach ($b in $BadStrings) {
    if ($txt.Contains($b)) {
      $RemainingBad += [PSCustomObject]@{ Path = $sf.FullName; BadString = $b }
    }
  }
}

$Obj = [ordered]@{
  schema = "CLD2/alpha51_2_1_version_string_polish"
  created_at = $Now
  ok = ($RemainingBad.Count -eq 0)
  code_baseline = $CodeVersion
  benchmark_milestone = $BenchmarkMilestone
  result_root = $ResultRoot
  repo_root = $RepoRoot
  touched_count = $Touched.Count
  touched_files = @($Touched | ForEach-Object { $_.Path })
  remaining_bad_references = @($RemainingBad)
  report = $ReportPath
}

$Obj | ConvertTo-Json -Depth 8 | Set-Content -Path $JsonPath -Encoding UTF8

Write-Host ""
Write-Host "Patch report:"
Write-Host $ReportPath
Write-Host "Patch json:"
Write-Host $JsonPath
Write-Host "Remaining bad references:" $RemainingBad.Count

# Create lightweight review ZIP.
$Zip = Join-Path $ResultRoot "alpha51_2_1_synthetic_matrix_REVIEW_UPLOAD.zip"
$ReviewDir = Join-Path $ResultRoot "alpha51_2_1_review_upload"

if (Test-Path $ReviewDir) { Remove-Item $ReviewDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReviewDir -Force | Out-Null

# Full inventory
Get-ChildItem $ResultRoot -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.FullName -notlike "*$ReviewDir*" -and $_.Name -ne "alpha51_2_1_synthetic_matrix_REVIEW_UPLOAD.zip"
} | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ResultRoot.Length).TrimStart('\\')
    LengthBytes = $_.Length
    LengthMB = [Math]::Round($_.Length / 1MB, 3)
    LastWriteTime = $_.LastWriteTime
    Extension = $_.Extension
  }
} | Sort-Object LengthBytes -Descending | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "FULL_FILE_INVENTORY.csv")

# Copy review text files only.
$ReviewExt = @(".json", ".csv", ".md", ".html", ".txt", ".log", ".sha256")
Get-ChildItem $ResultRoot -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.FullName -notlike "*$ReviewDir*" -and
  $_.Name -ne "alpha51_2_1_synthetic_matrix_REVIEW_UPLOAD.zip" -and
  $ReviewExt -contains $_.Extension.ToLowerInvariant() -and
  $_.Length -le 25MB
} | ForEach-Object {
  $rel = $_.FullName.Substring($ResultRoot.Length).TrimStart('\\')
  $dest = Join-Path $ReviewDir $rel
  New-Item -ItemType Directory -Path (Split-Path $dest -Parent) -Force | Out-Null
  Copy-Item $_.FullName $dest -Force
}

@"
# CLD2 alpha51.2.1 lightweight review upload

This ZIP excludes heavy generated artifacts and caches.

Included:
- JSON/CSV/MD/HTML/TXT/LOG/SHA256 files
- full file inventory
- alpha51.2.1 version string polish report

Code baseline: $CodeVersion
Benchmark milestone: $BenchmarkMilestone
Created: $Now
"@ | Set-Content -Encoding UTF8 (Join-Path $ReviewDir "README_REVIEW_UPLOAD.md")

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $Zip -Force

Write-Host ""
Write-Host "Review ZIP:"
Write-Host $Zip
if (Test-Path $Zip) { Get-Item $Zip | Select-Object FullName, Length, LastWriteTime }

if ($RemainingBad.Count -gt 0) {
  Write-Host ""
  Write-Host "WARNING: remaining problematic references found. Review alpha51_2_1_version_string_polish.json."
}
