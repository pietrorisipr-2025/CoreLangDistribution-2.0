# Alpha55 testing

Run the public examples from the repo root after extracting this pack:

```powershell
$Root = "C:\Users\Pietro\Desktop\Progetti\Corelang\CorelangDistribution 2.0"
$Repo = Join-Path $Root "50_2_CORRECTED_WORK\CLD2_ALPHA50_2_GITHUB_REPO_CANDIDATE_MIT"
$OutRoot = Join-Path $Root "17\per test\alpha55_public_examples"

Set-Location $Repo

powershell -ExecutionPolicy Bypass -File .\scripts\alpha55_run_public_examples.ps1 `
  -RepoRoot $Repo `
  -OutRoot $OutRoot `
  -SizeMiB 64
```

Expected result:

- `alpha55_public_examples_summary.json` exists.
- `ALPHA55_PUBLIC_EXAMPLES_REPORT.md` exists.
- every scenario has `audit_ok=true` and `digest_ok=true`.
- a lightweight `alpha55_public_examples_REVIEW_UPLOAD.zip` is created.
