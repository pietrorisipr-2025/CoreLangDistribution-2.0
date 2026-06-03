# CoreLangDistribution 2.0 (CLD2)

**Status:** alpha / public review candidate 50.2  
**Implementation baseline:** alpha50.2, derived from the validated alpha45.8 engine  
**Generated:** 2026-06-03T19:24:58+00:00

CLD2 is an experimental **cost-aware update distribution planner** built around chunking, content reuse, object-store workflows and transparent benchmark reporting.

It is **not** a new compression algorithm and it is not a universal replacement for rsync, zsync, casync, OSTree, DVC, Docker/OCI or CDN systems.

## What CLD2 is for

CLD2 is meant to explore update-heavy distribution cases where users already have a previous release/cache and the next release can reuse chunks/objects.

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

## Current validated result

The strongest validated result is the alpha45.8 fresh same-run comparison against zsync. CLD2 wins clearly in high-reuse cases and is roughly equal in adversarial cases.

| Scenario | zsync HTTP bytes | CLD2 warm bytes | Reading |
|---|---:|---:|---|
| normal | 239,827 | 419 | CLD2 clearly lower transfer |
| high-entropy | 67,342,559 | 67,108,937 | roughly equal/adversarial |
| small-files | 3,515,724 | 2,147 | CLD2 clearly lower transfer |
| heavy-change | 27,078,879 | 26,845,855 | roughly equal/heavy-change |

See [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) and [`docs/CLAIM_BOUNDARY.md`](docs/CLAIM_BOUNDARY.md).

## Quick start

Run the built-in self-test:

```bash
python cld2.py selftest
python cld2.py dist-check . --run-selftest
```

Create and compare two tiny releases:

```bash
python examples/small_demo/make_demo_data.py
python cld2.py pack examples/small_demo/release_v1 --out examples/small_demo/release_v1.cldrepo --release-id demo --release-seq 1 --force
python cld2.py pack examples/small_demo/release_v2 --out examples/small_demo/release_v2.cldrepo --release-id demo --release-seq 2 --force
python cld2.py diff examples/small_demo/release_v1.cldrepo examples/small_demo/release_v2.cldrepo --out examples/small_demo/diff.json
python cld2.py fetch examples/small_demo/release_v2.cldrepo --install examples/small_demo/install_v2 --cache examples/small_demo/cache
python cld2.py audit-install examples/small_demo/release_v2.cldrepo examples/small_demo/install_v2
```

Or run the smoke test:

```bash
python scripts/smoke_test.py
```

PowerShell:

```powershell
python .\scripts\smoke_test.py
```

## Repository layout

```text
cld2.py                  CLI entry point
corelangdistribution2/   reference implementation
tests/                   minimal tests
docs/                    public docs and benchmark summaries
data/                    benchmark matrix data
examples/small_demo/     tiny local demo
scripts/                 smoke/manifest helpers
.github/workflows/       CI self-test workflow
```

## Support

CLD2 is free and MIT-licensed.

If you find this project useful and want to support future development, you can do so here:

[Support development on Ko-fi](https://ko-fi.com/pietrorisi)

Support is optional and does not affect access to the code, releases or documentation.

## License

MIT License. See [LICENSE](LICENSE).

## Validation status

This alpha50.2 candidate was checked with `selftest`, `dist-check --run-selftest`, and `scripts/smoke_test.py`. See `TEST_RESULTS_ALPHA50_2_GITHUB_REPO_CANDIDATE_MIT.json`.