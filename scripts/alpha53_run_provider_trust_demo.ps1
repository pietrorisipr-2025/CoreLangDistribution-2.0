param(
  [Parameter(Mandatory=$true)]
  [string]$Alpha52ResultRoot,

  [Parameter(Mandatory=$true)]
  [string]$OutRoot
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null

$Verified = Join-Path $Alpha52ResultRoot "alpha52_1_provider_result_verified.json"
if (!(Test-Path $Verified)) {
  throw "Missing alpha52 verified result: $Verified"
}

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Validator = Join-Path $ScriptRoot "alpha53_validate_provider_result.py"
if (!(Test-Path $Validator)) {
  throw "Missing validator script: $Validator"
}

$Raw = Get-Content $Verified -Raw | ConvertFrom-Json
$Expected = $Raw.expected_sha256
if (-not $Expected) {
  $Expected = $Raw.expected_digest
}
if (-not $Expected) {
  throw "No expected_sha256/expected_digest found in alpha52 verified result."
}

$Policy = Join-Path $OutRoot "alpha53_provider_policy.json"
@{
  schema = "CLD2/alpha53_provider_policy"
  code_baseline = "2.0.0-alpha50.2"
  benchmark_milestone = "alpha53"
  require_ok = $true
  require_digest_ok = $true
  require_audit_install_ok = $true
  expected_sha256 = $Expected
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $Policy

$Positive = Join-Path $OutRoot "alpha53_positive_policy_validation.json"
$NegDigest = Join-Path $OutRoot "alpha53_negative_tampered_digest.json"
$NegAudit = Join-Path $OutRoot "alpha53_negative_audit_failure.json"

python $Validator --provider-result $Verified --policy $Policy --out $Positive --mode positive
$posRc = $LASTEXITCODE

python $Validator --provider-result $Verified --policy $Policy --out $NegDigest --mode tampered-digest
$negDigestRc = $LASTEXITCODE

python $Validator --provider-result $Verified --policy $Policy --out $NegAudit --mode audit-failure
$negAuditRc = $LASTEXITCODE

$pos = Get-Content $Positive -Raw | ConvertFrom-Json
$nd = Get-Content $NegDigest -Raw | ConvertFrom-Json
$na = Get-Content $NegAudit -Raw | ConvertFrom-Json

$Summary = [ordered]@{
  schema = "CLD2/alpha53_provider_trust_demo_summary"
  code_baseline = "2.0.0-alpha50.2"
  benchmark_milestone = "alpha53"
  alpha52_result_root = $Alpha52ResultRoot
  verified_result = $Verified
  policy = $Policy
  positive_validation_ok = [bool]$pos.ok
  tampered_digest_rejected = -not [bool]$nd.ok
  audit_failure_rejected = -not [bool]$na.ok
  positive_exit_code = $posRc
  tampered_digest_exit_code = $negDigestRc
  audit_failure_exit_code = $negAuditRc
  overall_ok = ([bool]$pos.ok -and (-not [bool]$nd.ok) -and (-not [bool]$na.ok))
  expected_sha256 = $Expected
  outputs = @($Positive, $NegDigest, $NegAudit)
}

$SummaryPath = Join-Path $OutRoot "alpha53_trust_demo_summary.json"
$Summary | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $SummaryPath

@"
# CLD2 alpha53 provider trust demo

## Verdict

- Positive validation OK: $($Summary.positive_validation_ok)
- Tampered digest rejected: $($Summary.tampered_digest_rejected)
- Audit failure rejected: $($Summary.audit_failure_rejected)
- Overall OK: $($Summary.overall_ok)

## Input

Alpha52 result root:

```text
$Alpha52ResultRoot
```

Verified result:

```text
$Verified
```

## Outputs

```text
$Positive
$NegDigest
$NegAudit
$SummaryPath
```
"@ | Set-Content -Encoding UTF8 (Join-Path $OutRoot "ALPHA53_PROVIDER_TRUST_DEMO_REPORT.md")

Write-Host "Alpha53 trust demo summary:"
Get-Content $SummaryPath -Raw

if (-not $Summary.overall_ok) {
  exit 2
}
