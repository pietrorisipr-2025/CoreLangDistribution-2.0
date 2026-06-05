param(
  [Parameter(Mandatory=$true)][string]$OutRoot,
  [string]$ZipName = "alpha51_1_REVIEW_UPLOAD.zip",
  [int]$MaxSingleFileMB = 25
)

$ErrorActionPreference = "Stop"

if (!(Test-Path $OutRoot)) {
  throw "OutRoot not found: $OutRoot"
}

$ReviewDir = Join-Path $OutRoot "_review_upload"
$Zip = Join-Path $OutRoot $ZipName

if (Test-Path $ReviewDir) { Remove-Item $ReviewDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReviewDir -Force | Out-Null

$AllowedExt = @(".json", ".csv", ".md", ".html", ".txt", ".log", ".sha256")
$MaxSingleFileBytes = $MaxSingleFileMB * 1MB

$Inventory = Get-ChildItem $OutRoot -Recurse -File | Where-Object {
  $_.FullName -notlike "$ReviewDir*" -and $_.FullName -ne $Zip
} | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($OutRoot.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthMB = [Math]::Round($_.Length / 1MB, 3)
    Extension = $_.Extension
    LastWriteTime = $_.LastWriteTime
  }
}

$Inventory | Sort-Object LengthBytes -Descending | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "FULL_FILE_INVENTORY.csv")

$FilesToCopy = Get-ChildItem $OutRoot -Recurse -File | Where-Object {
  $_.FullName -notlike "$ReviewDir*" -and
  $_.FullName -ne $Zip -and
  ($AllowedExt -contains $_.Extension.ToLower()) -and
  $_.Length -le $MaxSingleFileBytes
}

foreach ($File in $FilesToCopy) {
  $Rel = $File.FullName.Substring($OutRoot.Length).TrimStart('\')
  $Dest = Join-Path $ReviewDir $Rel
  $DestParent = Split-Path $Dest -Parent
  New-Item -ItemType Directory -Path $DestParent -Force | Out-Null
  Copy-Item $File.FullName $Dest -Force
}

Get-ChildItem $ReviewDir -Recurse -File | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ReviewDir.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthKB = [Math]::Round($_.Length / 1KB, 2)
  }
} | Sort-Object RelativePath | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "REVIEW_FILE_INVENTORY.csv")

@"
# CLD2 alpha51.1 lightweight review upload

This ZIP intentionally excludes generated artifact payloads, cache directories and heavy `.cldrepo` data.

Included:
- JSON results
- CSV summaries
- Markdown/HTML reports
- logs
- checksum files
- full file inventory

Source directory:
$OutRoot

Created:
$(Get-Date -Format o)
"@ | Set-Content -Encoding UTF8 (Join-Path $ReviewDir "README_REVIEW_UPLOAD.md")

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $Zip -Force

Write-Host "Review ZIP created:"
Write-Host $Zip
Get-Item $Zip | Select-Object FullName, Length, LastWriteTime
