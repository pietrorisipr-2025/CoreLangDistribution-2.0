# CLD2 alpha55 — Public Examples / Showcase Pack

Alpha55 adds a lightweight public showcase harness for CoreLangDistribution 2.0.

It does **not** change CLD2 core logic. It adds examples, docs and scripts to demonstrate CLD2 on artifact-shaped workloads that are easier to explain on GitHub:

- `game_asset_patch`
- `model_profile_pack`
- `ci_artifact_bundle`

The purpose is communication and practical evaluation, not a new cloud/CDN benchmark.

## Baseline

- Code baseline: `2.0.0-alpha50.2`
- Benchmark/demo milestone: `alpha55`
- Claim boundary: local generated examples only; not production validation.

## Main commands after extraction into the repo

```powershell
$Root = "C:\Users\Pietro\Desktop\Progetti\Corelang\CorelangDistribution 2.0"
$Repo = Join-Path $Root "50_2_CORRECTED_WORK\CLD2_ALPHA50_2_GITHUB_REPO_CANDIDATE_MIT"
$OutRoot = Join-Path $Root "17\per test\alpha55_public_examples"

powershell -ExecutionPolicy Bypass -File .\scripts\alpha55_run_public_examples.ps1 `
  -RepoRoot $Repo `
  -OutRoot $OutRoot `
  -SizeMiB 64
```

The runner creates:

```text
alpha55_public_examples_REVIEW_UPLOAD.zip
```

