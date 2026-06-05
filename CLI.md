# CLD2 CLI notes

After local installation:

```bash
python -m pip install -e .
cld2 --help
```

From a source checkout without installation:

```bash
python cld2.py --help
```

## Core commands

```bash
cld2 selftest
cld2 dist-check . --run-selftest
cld2 pack INPUT_DIR --out release.cldrepo --release-id demo --release-seq 1 --force
cld2 inspect release.cldrepo
cld2 verify release.cldrepo --deep
cld2 fetch release.cldrepo --install install_dir --cache cache_dir
cld2 audit-install release.cldrepo --install install_dir
cld2 diff old.cldrepo new.cldrepo --out diff.json
```

## Release-candidate verification

```bash
python scripts/verify_release.py --fast
python scripts/verify_release.py
```

`--fast` checks the manifest, imports and distribution hygiene. The full run also executes the embedded self-tests and small smoke demo.

## Benchmark/report commands

```bash
cld2 bench-real --help
cld2 bench-fastcdc-tune --help
cld2 bench-largefile-variants --help
cld2 bench-cost-aware-planner --help
cld2 render-report --help
```

Benchmark commands are research/development tools. Treat their results as workload-specific measurements, not universal performance claims.
