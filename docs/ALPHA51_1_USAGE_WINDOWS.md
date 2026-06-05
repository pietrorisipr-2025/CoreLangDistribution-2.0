# Alpha51.1 Windows usage

Run commands from the CLD2 repository root, the folder that contains:

```text
cld2.py
README.md
corelangdistribution2\
scripts\
```

Expected base version:

```text
2.0.0-alpha50.2
```

## 1. Generate synthetic artifacts

```powershell
$Root = "C:\Users\Pietro\Desktop\Progetti\Corelang\CorelangDistribution 2.0"
$Repo = Join-Path $Root "50_2\CLD2_ALPHA50_2_GITHUB_REPO_CANDIDATE_MIT"
$SyntheticRoot = Join-Path $Root "17\per test\alpha51_1_synthetic_artifacts"

Set-Location $Repo

python .\scripts\alpha51_1_make_synthetic_artifacts.py `
  --out-dir $SyntheticRoot `
  --size-mib 64
```

## 2. Run synthetic matrix

```powershell
$Root = "C:\Users\Pietro\Desktop\Progetti\Corelang\CorelangDistribution 2.0"
$Repo = Join-Path $Root "50_2\CLD2_ALPHA50_2_GITHUB_REPO_CANDIDATE_MIT"
$SyntheticRoot = Join-Path $Root "17\per test\alpha51_1_synthetic_artifacts"
$OutRoot = Join-Path $Root "17\per test\alpha51_1_synthetic_matrix"

Set-Location $Repo

powershell -ExecutionPolicy Bypass -File .\scripts\alpha51_1_run_synthetic_matrix.ps1 `
  -SyntheticRoot $SyntheticRoot `
  -OutRoot $OutRoot
```

## 3. Upload only the review ZIP

Upload:

```text
C:\Users\Pietro\Desktop\Progetti\Corelang\CorelangDistribution 2.0\17\per test\alpha51_1_synthetic_matrix\alpha51_1_synthetic_matrix_REVIEW_UPLOAD.zip
```

Do not upload the full artifact/caches ZIP unless explicitly requested.
