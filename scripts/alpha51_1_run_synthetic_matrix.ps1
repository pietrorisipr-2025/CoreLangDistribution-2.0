param(
  [Parameter(Mandatory=$true)][string]$SyntheticRoot,
  [Parameter(Mandatory=$true)][string]$OutRoot
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Get-Location).Path
$Runner = Join-Path $RepoRoot "scripts\alpha51_1_run_real_artifact_benchmark.ps1"
$Summarizer = Join-Path $RepoRoot "scripts\alpha51_1_summarize_results.py"
$ReviewMaker = Join-Path $RepoRoot "scripts\alpha51_1_make_review_zip.ps1"

if (!(Test-Path $Runner)) { throw "Missing runner: $Runner" }
if (!(Test-Path $Summarizer)) { throw "Missing summarizer: $Summarizer" }
if (!(Test-Path $ReviewMaker)) { throw "Missing review maker: $ReviewMaker" }
if (!(Test-Path $SyntheticRoot)) { throw "SyntheticRoot not found: $SyntheticRoot" }

$Cases = @(
  @{ Name = "domain_pack_mixed_high_reuse"; Profile = "medium"; Chunker = "both"; Codec = "auto" },
  @{ Name = "model_pack_localized_random"; Profile = "large-file-balanced"; Chunker = "both"; Codec = "auto" },
  @{ Name = "many_small_mixed_update"; Profile = "medium"; Chunker = "both"; Codec = "auto" },
  @{ Name = "badfit_entropy_rewrite"; Profile = "large-file-balanced"; Chunker = "both"; Codec = "auto" }
)

New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null
$MatrixLog = Join-Path $OutRoot "alpha51_1_synthetic_matrix_command_log.txt"
@"
=== CLD2 alpha51.1 synthetic matrix ===
Date: $(Get-Date -Format o)
RepoRoot: $RepoRoot
SyntheticRoot: $SyntheticRoot
OutRoot: $OutRoot
"@ | Set-Content -Encoding UTF8 $MatrixLog

foreach ($Case in $Cases) {
  $Name = $Case.Name
  $Old = Join-Path $SyntheticRoot "$Name\v1"
  $New = Join-Path $SyntheticRoot "$Name\v2"
  $Out = Join-Path $OutRoot $Name
  Write-Host "=== alpha51.1 synthetic case: $Name ==="
  "=== alpha51.1 synthetic case: $Name ===" | Tee-Object -Append $MatrixLog
  powershell -ExecutionPolicy Bypass -File $Runner `
    -OldDir $Old `
    -NewDir $New `
    -OutDir $Out `
    -ScenarioName $Name `
    -Profile $Case.Profile `
    -Chunker $Case.Chunker `
    -Codec $Case.Codec `
    -NoReviewZip 2>&1 | Tee-Object -Append $MatrixLog
}

python $Summarizer --out-root $OutRoot 2>&1 | Tee-Object -Append $MatrixLog

powershell -ExecutionPolicy Bypass -File $ReviewMaker `
  -OutRoot $OutRoot `
  -ZipName "alpha51_1_synthetic_matrix_REVIEW_UPLOAD.zip" 2>&1 | Tee-Object -Append $MatrixLog

$Zip = Join-Path $OutRoot "alpha51_1_synthetic_matrix_REVIEW_UPLOAD.zip"
Write-Host ""
Write-Host "Review ZIP to upload:"
Write-Host $Zip
if (Test-Path $Zip) { Get-Item $Zip | Select-Object FullName, Length, LastWriteTime }
