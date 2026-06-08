# CoreLangDistribution 2.0 (CLD2)

**Status:** alpha / public review candidate 56.3  
**Python package version:** `2.0.0a56.post3`  
**Implementation baseline:** `2.0.0-alpha50.2` core; alpha56.3 benchmark tooling/profile/docs patch  
**Generated:** 2026-06-07

CLD2 is an experimental **cost-aware warm-update distribution planner**. It packages versioned content into chunked, range-readable repositories so clients with an existing release/cache can reuse chunks instead of downloading the whole next release.

It is **not** a new compression algorithm, not a CDN/cloud product, and not a universal replacement for rsync, zsync, casync, OSTree, DVC, Docker/OCI or mature artifact distribution systems.

## Try it in 5 minutes

Requirements:

- Python 3.10+
- `pip`

From a fresh checkout:

```bash
python -m pip install -e .
cld2 --version
cld2 selftest
python scripts/verify_release.py --fast
python scripts/smoke_test.py
```

On Windows, use the same commands from PowerShell if `python` is on PATH. If your system uses the Python launcher, replace `python` with `py -3`.

What this does:

- installs the local package and the `cld2` command;
- runs the embedded self-test;
- checks the source tree manifest/import/dist hygiene;
- runs a tiny demo that creates, compares, fetches and audits two releases, then cleans generated demo artifacts.

For a full release-candidate check, run:

```bash
python scripts/verify_release.py
```

## What CLD2 is for

CLD2 is meant to explore update-heavy distribution cases where users already have a previous release/cache and the next release can reuse chunks or objects.

Useful target areas:

- release/update distribution;
- object-store based delivery;
- CI/CD artifacts;
- game/data/model update workflows;
- FinOps-style transfer cost analysis;
- benchmark research against existing tools.

## What CLD2 is not

CLD2 is not:

- a generic compressor;
- a better zstd/gzip;
- a proven CDN/cloud product;
- enterprise-ready;
- a claim that CLD2 always beats existing tools.

## Real benchmark status

Post-alpha56.2.1 real benchmarks found a mixed but useful pattern:

- strongest positive: AMD RDNA extracted driver payload, where CLD2 fixed-mode warm update was smaller than both full v2 tar.zstd and changed-files tar.zstd;
- moderate positive: LibreOffice extracted folders, where CLD2 beat full v2 tar.zstd but not changed-files tar.zstd;
- negative controls: melonDS single-executable releases and SDSS FITS DR11->DR12 did not produce a competitive CLD2 update.

These results support a targeted structured-payload warm-update claim, not a universal updater/compressor claim. See [`docs/REAL_BENCHMARKS_ALPHA56_3.md`](docs/REAL_BENCHMARKS_ALPHA56_3.md), [`docs/BENCHMARK_CLAIM_BOUNDARY_ALPHA56_3.md`](docs/BENCHMARK_CLAIM_BOUNDARY_ALPHA56_3.md), and [`docs/BENCHMARK_TOOLING_NOTES_ALPHA56_3.md`](docs/BENCHMARK_TOOLING_NOTES_ALPHA56_3.md).

## Historical validated result

The strongest validated result is still the alpha45.8 same-run comparison against zsync. CLD2 wins clearly in high-reuse cases and is roughly equal in adversarial cases.

| Scenario | zsync HTTP bytes | CLD2 warm bytes | Reading |
|---|---:|---:|---|
| normal | 239,827 | 419 | CLD2 clearly lower transfer |
| high-entropy | 67,342,559 | 67,108,937 | roughly equal/adversarial |
| small-files | 3,515,724 | 2,147 | CLD2 clearly lower transfer |
| heavy-change | 27,078,879 | 26,845,855 | roughly equal/heavy-change |

See [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md), [`docs/BENCHMARK_SUMMARY_UPDATED_ALPHA55.md`](docs/BENCHMARK_SUMMARY_UPDATED_ALPHA55.md) and [`docs/CLAIM_BOUNDARY_ALPHA56.md`](docs/CLAIM_BOUNDARY_ALPHA56.md).

## User-defined profiles

CLD2 alpha56.3 supports JSON profile files for reusable chunker/codec settings.

Example:

```bash
cld2 profile-validate docs/profiles/amd-rdna-extracted-fixed-balanced.json
cld2 pack path/to/release --out release.cldrepo --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json --force
cld2 bench-real --old-dir old --new-dir new --out-dir results --profile-file docs/profiles/amd-rdna-extracted-fixed-balanced.json
```

Built-in examples live in [`docs/profiles/`](docs/profiles/). Alpha56.3 accepts JSON only and rejects unknown top-level profile keys.

## Common commands

```bash
cld2 pack INPUT_DIR --out release.cldrepo --release-id demo --release-seq 1 --force
cld2 profile-validate docs/profiles/amd-rdna-extracted-fixed-balanced.json
cld2 inspect release.cldrepo
cld2 verify release.cldrepo --deep
cld2 fetch release.cldrepo --install install_dir --cache cache_dir
cld2 audit-install release.cldrepo --install install_dir
cld2 diff old.cldrepo new.cldrepo --out diff.json
```

The source checkout can also be used without installation:

```bash
python cld2.py selftest
python cld2.py dist-check . --run-selftest
python scripts/smoke_test.py
```

To keep generated demo releases/install/cache for inspection:

```bash
python scripts/smoke_test.py --keep-demo
```

## Repository layout

```text
cld2.py                  CLI entry point
corelangdistribution2/   reference implementation
tests/                   focused regression/self-test coverage
docs/                    public docs, benchmarks and claim boundary
data/                    benchmark matrix data
examples/small_demo/     tiny local demo
scripts/                 smoke, manifest and release verification helpers
.github/workflows/       CI self-test workflow
```

## Public review notes

Alpha56.3 replaces alpha56.2.1 for public review. It fixes benchmark tooling/reporting, adds JSON user-defined profiles, and documents real benchmark results. It does not change the core transfer model.

Important version note: generated `.cldrepo` manifests may contain an internal repository-format `version` such as `2.0-alpha24`. That is a repository schema version, not the Python package version. The Python package version is the PEP 440 value `2.0.0a56.post3`; the core implementation baseline remains `2.0.0-alpha50.2`.

## License

MIT License. See [LICENSE](LICENSE).

## Validation status

This candidate is designed to be checked with:

```bash
python scripts/verify_release.py
```

The alpha56.1 predecessor was checked with `selftest`, `dist-check --run-selftest`, and `scripts/smoke_test.py`; see `TEST_RESULTS_ALPHA56_1_POLISH.json`.
