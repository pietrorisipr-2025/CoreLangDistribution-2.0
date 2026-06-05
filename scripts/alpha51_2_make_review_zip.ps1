param(
  [Parameter(Mandatory=$true)][string]$ResultRoot,
  [string]$ZipName = "alpha51_2_synthetic_matrix_REVIEW_UPLOAD.zip"
)

if (!(Test-Path $ResultRoot)) {
  Write-Host "ERRORE: ResultRoot non trovato: $ResultRoot"
  exit 1
}

$ReviewDir = Join-Path $ResultRoot "alpha51_2_REVIEW_UPLOAD"
$Zip = Join-Path $ResultRoot $ZipName

if (Test-Path $ReviewDir) { Remove-Item $ReviewDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReviewDir -Force | Out-Null

$AllowedExt = @(".json", ".csv", ".md", ".html", ".txt", ".log", ".sha256")
$MaxSingleFileBytes = 25MB

$Inventory = Get-ChildItem $ResultRoot -Recurse -File | Where-Object {
  $_.FullName -notlike "*$ReviewDir*" -and $_.FullName -ne $Zip
} | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ResultRoot.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthMB = [Math]::Round($_.Length / 1MB, 3)
    LastWriteTime = $_.LastWriteTime
    Extension = $_.Extension
  }
}

$Inventory | Sort-Object LengthBytes -Descending | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "FULL_FILE_INVENTORY.csv")

$FilesToCopy = Get-ChildItem $ResultRoot -Recurse -File | Where-Object {
  $_.FullName -notlike "*$ReviewDir*" -and
  $_.FullName -ne $Zip -and
  ($AllowedExt -contains $_.Extension.ToLower()) -and
  $_.Length -le $MaxSingleFileBytes
}

foreach ($File in $FilesToCopy) {
  $Rel = $File.FullName.Substring($ResultRoot.Length).TrimStart('\')
  $Dest = Join-Path $ReviewDir $Rel
  $Parent = Split-Path $Dest -Parent
  New-Item -ItemType Directory -Path $Parent -Force | Out-Null
  Copy-Item $File.FullName $Dest -Force
}

@"
# CLD2 alpha51.2 lightweight review upload

This ZIP excludes generated repositories, caches and large synthetic artifacts.

Source ResultRoot:
$ResultRoot

Created:
$(Get-Date -Format o)
"@ | Set-Content -Encoding UTF8 (Join-Path $ReviewDir "README_REVIEW_UPLOAD.md")

Get-ChildItem $ReviewDir -Recurse -File | ForEach-Object {
  [PSCustomObject]@{
    RelativePath = $_.FullName.Substring($ReviewDir.Length).TrimStart('\')
    LengthBytes = $_.Length
    LengthKB = [Math]::Round($_.Length / 1KB, 2)
  }
} | Sort-Object RelativePath | Export-Csv -NoTypeInformation -Encoding UTF8 -Path (Join-Path $ReviewDir "REVIEW_FILE_INVENTORY.csv")

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $Zip -Force

Write-Host "ZIP review creato:"
Write-Host $Zip
Get-Item $Zip | Select-Object FullName, Length, LastWriteTime
