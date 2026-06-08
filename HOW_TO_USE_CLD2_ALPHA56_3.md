# How to use CLD2 alpha56.3

## Install

```bash
python -m pip install -e .
cld2 --version
```

Expected version:

```text
2.0.0a56.post3
```

## Verify the checkout

Fast check:

```bash
python scripts/verify_release.py --fast
```

Full check:

```bash
python scripts/verify_release.py
```

## Basic workflow

```bash
cld2 pack path/to/release_v1 --out release_v1.cldrepo --release-id demo --release-seq 1 --force
cld2 pack path/to/release_v2 --out release_v2.cldrepo --release-id demo --release-seq 2 --force
cld2 diff release_v1.cldrepo release_v2.cldrepo --out diff.json
cld2 verify release_v2.cldrepo --deep
cld2 fetch release_v2.cldrepo --install install_v2 --cache cache_dir
cld2 audit-install release_v2.cldrepo --install install_v2
```

## JSON profiles

```bash
cld2 profile-validate docs/profiles/amd-rdna-extracted-fixed-balanced.json
cld2 pack path/to/release --out release.cldrepo --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json --force
cld2 bench-real --old-dir old --new-dir new --out-dir results --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json
```

`--profile-file` uses JSON only. For `pack`, do not combine it with explicit chunker/codec/chunk-size options. For `bench-real`, do not combine it with explicit `--profile`, `--chunker`, or `--codec`.

## Review ZIP from benchmark results

```bash
cld2 make-review-zip --src-dir results --zip-out results_REVIEW.zip --max-mb 10
```

alpha56.3 includes `bench-real` public report outputs and excludes generated `.cldrepo`, `packs`, `cache`, `install`, and internal index files.
