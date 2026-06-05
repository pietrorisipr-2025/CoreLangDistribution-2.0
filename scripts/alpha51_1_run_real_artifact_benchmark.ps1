param(
  [Parameter(Mandatory=$true)][string]$OldDir,
  [Parameter(Mandatory=$true)][string]$NewDir,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [string]$ScenarioName = "real_artifact_v1_to_v2",
  [string]$Profile = "large-file-balanced",
  [string]$Chunker = "both",
  [string]$Codec = "auto",
  [int]$DownloadCount = 1000,
  [double]$CostPerGb = 0.09,
  [string]$Currency = "USD",
  [switch]$NoReviewZip,
  [switch]$IncludeHeavyZip
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Get-Location).Path
$Cld2 = Join-Path $RepoRoot "cld2.py"
if (!(Test-Path $Cld2)) { throw "cld2.py not found. Run this script from the CLD2 repository root." }
if (!(Test-Path $OldDir)) { throw "OldDir not found: $OldDir" }
if (!(Test-Path $NewDir)) { throw "NewDir not found: $NewDir" }

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$Log = Join-Path $OutDir "alpha51_1_bench_real_command_log.txt"
$Meta = Join-Path $OutDir "alpha51_1_bench_real_metadata.json"

@"
=== CLD2 alpha51.1 real artifact benchmark ===
Date: $(Get-Date -Format o)
RepoRoot: $RepoRoot
OldDir: $OldDir
NewDir: $NewDir
OutDir: $OutDir
ScenarioName: $ScenarioName
Profile: $Profile
Chunker: $Chunker
Codec: $Codec
"@ | Set-Content -Encoding UTF8 $Log

$Cmd = @(
  "cld2.py", "bench-real",
  "--old-dir", $OldDir,
  "--new-dir", $NewDir,
  "--out-dir", $OutDir,
  "--scenario-name", $ScenarioName,
  "--profile", $Profile,
  "--chunker", $Chunker,
  "--codec", $Codec,
  "--download-count", "$DownloadCount",
  "--cost-per-gb", "$CostPerGb",
  "--currency", $Currency
)

"Command: python $($Cmd -join ' ')" | Tee-Object -Append $Log
python @Cmd 2>&1 | Tee-Object -Append $Log

$Obj = [ordered]@{
  schema = "CLD2/alpha51_1_real_artifact_benchmark_wrapper"
  created_at = (Get-Date -Format o)
  repo_root = $RepoRoot
  old_dir = $OldDir
  new_dir = $NewDir
  out_dir = $OutDir
  scenario_name = $ScenarioName
  profile = $Profile
  chunker = $Chunker
  codec = $Codec
  benchmark_kind = "alpha51.1 real artifact benchmark wrapper, not a CLD2 software release"
  claim_boundary = "Interpret results per artifact; do not generalize to all workloads. Compare against best compressed conventional baseline, not just raw transfer."
}
$Obj | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $Meta

# Scenario-level summary/classification.
$Summarizer = Join-Path $RepoRoot "scripts\alpha51_1_summarize_results.py"
if (Test-Path $Summarizer) {
  python $Summarizer --out-root $OutDir 2>&1 | Tee-Object -Append $Log
} else {
  "Summarizer not found: $Summarizer" | Tee-Object -Append $Log
}

if ($IncludeHeavyZip) {
  $HeavyZip = Join-Path $OutDir "alpha51_1_real_artifact_FULL_UPLOAD.zip"
  if (Test-Path $HeavyZip) { Remove-Item $HeavyZip -Force }
  $Items = Get-ChildItem $OutDir -Force | Where-Object { $_.Name -ne "alpha51_1_real_artifact_FULL_UPLOAD.zip" }
  if ($Items.Count -gt 0) {
    Compress-Archive -Path $Items.FullName -DestinationPath $HeavyZip -Force
    Write-Host "Heavy full ZIP created:"
    Write-Host $HeavyZip
  }
}

if (-not $NoReviewZip) {
  $ReviewMaker = Join-Path $RepoRoot "scripts\alpha51_1_make_review_zip.ps1"
  if (Test-Path $ReviewMaker) {
    powershell -ExecutionPolicy Bypass -File $ReviewMaker -OutRoot $OutDir -ZipName "alpha51_1_real_artifact_REVIEW_UPLOAD.zip"
  } else {
    Write-Host "Review maker not found: $ReviewMaker"
  }
}
